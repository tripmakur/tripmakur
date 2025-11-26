import os
import json
import time
from datetime import datetime
from io import BytesIO

import pandas as pd
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file

app = Flask(__name__)
app.secret_key = "your-secret-key"

# ================================
# Allowed Companies
# ================================
ALLOWED_COMPANIES_FILE = "allowed_companies.json"


def load_allowed_companies():
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        try:
            with open(ALLOWED_COMPANIES_FILE, "r") as f:
                companies = json.load(f)
                return [str(c).strip().lower() for c in companies]
        except Exception:
            return []
    return []


ALLOWED_COMPANIES = load_allowed_companies()
last_results = []            # for flight status download
last_analysis_results = []   # for flight analysis download


def is_company_allowed(company: str) -> bool:
    # If no companies configured, allow all
    if not ALLOWED_COMPANIES:
        return True
    if not company:
        return False
    return company.lower() in ALLOWED_COMPANIES


# ================================
# FlightAPI.io Settings (Status)
# ================================
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY", "69175603253bb1627f7ea9cc")


def fetch_status_flightapi(airline: str, flight_number: str, flight_date: str):
    """
    Fetch a flight’s status and estimated times from FlightAPI.io.

    Supports:
    - airline codes in ANY format (DL, dl, Dl, dL)
    - list-style API responses
    - dict responses containing "flights"
    - estimated times for delayed flights
    """

    # Always convert airline code to uppercase to avoid failures
    airline_code = airline.strip().upper()
    flight_number = str(flight_number).strip()
    date_str = flight_date.replace("-", "")

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline_code}&date={date_str}"
    )

    try:
        resp = requests.get(url, timeout=20)

        if resp.status_code != 200:
            return {
                "Status": f"API Error ({resp.status_code})",
                "EstimatedDeparture": "-",
                "EstimatedArrival": "-",
            }

        data = resp.json()

        # ----------------------------------------------
        # CASE 1 — API returns simple list structure
        # ----------------------------------------------
        if isinstance(data, list):
            status = "Unknown"
            est_dep = "-"
            est_arr = "-"

            for block in data:

                # Extract status
                if "status" in block:
                    status = block.get("status", "Unknown")

                # Extract departure estimated times
                if "departure" in block:
                    est_dep = (
                        block["departure"].get("estimatedTime")
                        or block["departure"].get("scheduledTime")
                        or "-"
                    )

                # Extract arrival estimated times
                if "arrival" in block:
                    est_arr = (
                        block["arrival"].get("estimatedTime")
                        or block["arrival"].get("scheduledTime")
                        or "-"
                    )

            return {
                "Status": status,
                "EstimatedDeparture": est_dep,
                "EstimatedArrival": est_arr,
            }

        # ----------------------------------------------
        # CASE 2 — API returns: {"flights": [...]}
        # ----------------------------------------------
        if isinstance(data, dict) and "flights" in data:
            flights = data.get("flights") or []

            if not flights:
                return {
                    "Status": "Not Found",
                    "EstimatedDeparture": "-",
                    "EstimatedArrival": "-",
                }

            f = flights[0]

            # Convert numeric status codes
            raw_status = f.get("displayStatus") or f.get("status")
            if isinstance(raw_status, int):
                status_map = {
                    1: "Scheduled",
                    2: "Arrived",
                    3: "Departed",
                    4: "Delayed",
                    5: "Cancelled",
                }
                status = status_map.get(raw_status, "Unknown")
            else:
                status = raw_status or "Unknown"

            # Get estimated/scheduled times safely
            est_dep = f.get("estimatedDepartureTime") or f.get("departureTime") or "-"
            est_arr = f.get("estimatedArrivalTime") or f.get("arrivalTime") or "-"

            return {
                "Status": status,
                "EstimatedDeparture": est_dep,
                "EstimatedArrival": est_arr,
            }

        # ----------------------------------------------
        # FALLBACK CASE
        # ----------------------------------------------
        return {
            "Status": "Not Found",
            "EstimatedDeparture": "-",
            "EstimatedArrival": "-",
        }

    except Exception as e:
        return {
            "Status": f"Error: {str(e)}",
            "EstimatedDeparture": "-",
            "EstimatedArrival": "-",
        }



# ================================
# Amadeus Settings (Flight Analysis)
# ================================
# Use env vars if present, otherwise fall back to the keys you gave me
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID", "ZGOUVp1fU3EGHoNGEuNgjJDtunQ9GgNe")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET", "4oJvcvPX5qWd0dgh")

AMADEUS_AUTH_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
AMADEUS_FLIGHT_OFFERS_URL = "https://test.api.amadeus.com/v2/shopping/flight-offers"

amadeus_token = None
amadeus_token_expiry = 0  # epoch seconds


def get_amadeus_token():
    """Get or refresh Amadeus OAuth token."""
    global amadeus_token, amadeus_token_expiry

    if not AMADEUS_CLIENT_ID or not AMADEUS_CLIENT_SECRET:
        raise RuntimeError("Amadeus credentials not configured")

    now = time.time()
    if amadeus_token and now < amadeus_token_expiry:
        return amadeus_token

    data = {
        "grant_type": "client_credentials",
        "client_id": AMADEUS_CLIENT_ID,
        "client_secret": AMADEUS_CLIENT_SECRET,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    resp = requests.post(AMADEUS_AUTH_URL, data=data, headers=headers, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"Amadeus auth failed: {resp.status_code} {resp.text}")

    payload = resp.json()
    amadeus_token = payload.get("access_token")
    expires_in = payload.get("expires_in", 1800)
    amadeus_token_expiry = now + expires_in - 60  # refresh slightly early

    return amadeus_token


def search_lowest_fare_amadeus(origin, destination, departure_date, return_date):
    """
    Safe Amadeus wrapper that NEVER throws Flask errors.
    """

    # Step 1 — Try token request
    try:
        token = get_amadeus_token()
    except Exception as e:
        return {
            "Origin": origin,
            "Destination": destination,
            "Error": f"Auth failed: {str(e)}"
        }

    # Step 2 — Try fare search
    try:
        params = {
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDate": departure_date,
            "returnDate": return_date,
            "adults": 1,
            "currencyCode": "USD",
            "max": 1
        }

        headers = {"Authorization": f"Bearer {token}"}

        resp = requests.get(
            AMADEUS_FLIGHT_OFFERS_URL,
            headers=headers,
            params=params,
            timeout=30
        )

        if resp.status_code != 200:
            return {
                "Origin": origin,
                "Destination": destination,
                "Error": f"API error {resp.status_code} - {resp.text[:200]}"
            }

        data = resp.json()

        offers = data.get("data", [])
        if not offers:
            return {
                "Origin": origin,
                "Destination": destination,
                "Error": "No fares found"
            }

        price = offers[0].get("price", {})
        return {
            "Origin": origin,
            "Destination": destination,
            "Price": price.get("grandTotal"),
            "Currency": price.get("currency", "USD"),
            "Error": ""
        }

    except Exception as e:
        return {
            "Origin": origin,
            "Destination": destination,
            "Error": f"Request failed: {str(e)}"
        }



# ================================
# HOME PAGE
# ================================
@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        company = request.form.get("company", "").strip().lower()

        if not company:
            flash("Please enter a company name.")
            return render_template("index.html")

        if not is_company_allowed(company):
            flash(f"Access denied for company: {company}")
            return render_template("index.html")

        return redirect(url_for("flight_status", company=company))

    return render_template("index.html")


# ================================
# FLIGHT STATUS PAGE
# ================================
@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    global last_results

    company = (request.args.get("company") or request.form.get("company") or "").strip().lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    flight_info = None
    uploaded_results = None

    if request.method == "POST":
        airline = request.form.get("airline", "").strip().upper()
        flight_number = request.form.get("flight_number", "").strip()
        departure = request.form.get("departure", "").strip().upper()
        arrival = request.form.get("arrival", "").strip().upper()

        if not all([airline, flight_number, departure, arrival]):
            flash("All fields are required.")
            return render_template(
                "flight_status.html",
                company=company,
                flight_info=None,
                uploaded_results=None,
            )

        today = datetime.today().strftime("%Y-%m-%d")
        status = fetch_status_flightapi(airline, flight_number, today)

        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": departure,
            "To": arrival,
            "Status": status,
        }

        last_results = [flight_info]

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results,
    )


# ================================
# EXCEL UPLOAD (Status)
# ================================
@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results

    company = (request.args.get("company") or request.form.get("company") or "").strip().lower()
    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    if "file" not in request.files:
        flash("No file uploaded.")
        return redirect(url_for("flight_status", company=company))

    file = request.files["file"]
    if not file.filename:
        flash("No selected file.")
        return redirect(url_for("flight_status", company=company))

    try:
        if file.filename.lower().endswith(".csv"):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file, sheet_name=0)

        df.columns = df.columns.str.strip().str.lower()

        required = ["airline", "flightnumber", "departure", "arrival"]
        missing = [c for c in required if c not in df.columns]

        if missing:
            flash(f"Missing required columns: {missing}")
            return redirect(url_for("flight_status", company=company))

        today = datetime.today().strftime("%Y-%m-%d")
        results = []

        for _, row in df.iterrows():
            airline = str(row["airline"]).strip().upper()
            flight_number = str(row["flightnumber"]).strip()
            dep = str(row["departure"]).strip().upper()
            arr = str(row["arrival"]).strip().upper()

            status = fetch_status_flightapi(airline, flight_number, today)

            results.append(
                {
                    "Airline": airline,
                    "FlightNumber": flight_number,
                    "From": dep,
                    "To": arr,
                    "Status": status,
                }
            )

        last_results = results

        return render_template(
            "flight_status.html",
            company=company,
            flight_info=None,
            uploaded_results=results,
        )

    except Exception as e:
        flash(f"Error processing file: {e}")
        return redirect(url_for("flight_status", company=company))


# ================================
# DOWNLOAD STATUS RESULTS
# ================================
@app.route("/download")
def download_excel():
    if not last_results:
        flash("No results to download.")
        return redirect(url_for("home"))

    df = pd.DataFrame(last_results)
    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(
        output,
        download_name="flight_status_results.xlsx",
        as_attachment=True,
    )


# ================================
# FLIGHT ANALYSIS PAGE (Amadeus)
# ================================
@app.route("/flight-analysis", methods=["GET", "POST"])
def flight_analysis():
    global last_analysis_results

    company = (
        request.args.get("company")
        or request.form.get("company")
        or ""
    ).strip().lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    analysis_results = None

    if request.method == "POST":

        # DEBUG LOGGING
        app.logger.info("=== POST RECEIVED ON /flight-analysis ===")
        app.logger.info(str(request.form))

        origins = request.form.getlist("origins")
        destinations = request.form.getlist("destinations")

        origins = [o.strip().upper() for o in origins if o.strip()]
        destinations = [d.strip().upper() for d in destinations if d.strip()]

        outbound_date = request.form.get("outbound_date", "").strip()
        return_date = request.form.get("return_date", "").strip()

        # VALIDATION
        if not origins or not destinations:
            flash("Please enter at least one origin and one destination.")
            return render_template("flight_analysis.html", company=company)

        if not outbound_date or not return_date:
            flash("Please select outbound and return dates.")
            return render_template("flight_analysis.html", company=company)

        # Perform analysis
        analysis_results = []
        for o in origins:
            for d in destinations:
                result = search_lowest_fare_amadeus(o, d, outbound_date, return_date)
                analysis_results.append({
                    "Origin": o,
                    "Destination": d,
                    "DepartureDate": outbound_date,
                    "ReturnDate": return_date,
                    "Price": result.get("Price"),
                    "Currency": result.get("Currency", "USD"),
                    "Error": result.get("Error", "")
                })

        last_analysis_results = analysis_results

    return render_template(
        "flight_analysis.html",
        company=company,
        analysis_results=analysis_results
    )

