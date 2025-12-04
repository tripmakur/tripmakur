import os
import json
import time
from datetime import datetime
from io import BytesIO

import pandas as pd
import requests
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_file,
)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "your-secret-key")

# ==============================
# Allowed Companies
# ==============================

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

# in-memory result buffers
last_results = []            # for flight status -> Download Excel
last_analysis_results = []   # for flight analysis download


def is_company_allowed(company: str) -> bool:
    """If allowed_companies.json is empty, allow any company."""
    if not ALLOWED_COMPANIES:
        return True
    if not company:
        return False
    return company.lower() in ALLOWED_COMPANIES


# ==============================
# FlightAPI.io Settings (STATUS)
# ==============================

FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY")


def fetch_status_and_times(airline: str, flight_number: str, flight_date: str):
    """
    Call FlightAPI.io's airline endpoint:

    https://api.flightapi.io/airline/{KEY}?num={flight_number}&name={airline}&date={YYYYMMDD}

    Returns a dict:
      {
        "Status": "Arrived" / "Scheduled" / "Delayed" / ...,
        "EstimatedDeparture": "14:40, Nov 26" or "",
        "EstimatedArrival": "17:06, Nov 26" or ""
      }
    """
    if not FLIGHTAPI_KEY:
        return {
            "Status": "API Error (Missing FLIGHTAPI_KEY)",
            "EstimatedDeparture": "",
            "EstimatedArrival": "",
        }

    airline_code = airline.strip().lower()  # matches example in docs
    date_str = flight_date.replace("-", "")  # YYYYMMDD

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline_code}&date={date_str}"
    )

    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            return {
                "Status": f"API Error ({resp.status_code})",
                "EstimatedDeparture": "",
                "EstimatedArrival": "",
            }

        data = resp.json()

        status_text = "Unknown"
        est_dep = ""
        est_arr = ""

        # Case 1: Airline endpoint returns a dict with "flights"
        # Example:
        # {
        #   "flights": [
        #       {
        #         "airlineCode": "DL",
        #         "flightNumber": 3758,
        #         "displayStatus": "Arrived",
        #         "departureTime": "09:29, Nov 26",
        #         "arrivalTime": "10:02, Nov 26",
        #         ...
        #       },
        #       ...
        #   ],
        #   "flight": null,
        #   "emptyResults": false
        # }
        if isinstance(data, dict) and "flights" in data:
            flights = data.get("flights") or []
            if flights:
                # Just take the first match for now
                f = flights[0]
                status_text = f.get("displayStatus") or "Unknown"

                # These are the times you showed from the API
                est_dep = f.get("departureTime") or ""
                est_arr = f.get("arrivalTime") or ""

        # Case 2: older list format: [ {departure}, {arrival}, {aircraft}, {status:"..."} ]
        elif isinstance(data, list):
            for block in data:
                if isinstance(block, dict) and "status" in block:
                    status_text = block.get("status") or "Unknown"
            # departure / arrival estimatedTime sometimes live in the first two blocks
            if len(data) >= 2:
                dep_block = data[0] or {}
                arr_block = data[1] or {}

                dep = dep_block.get("departure") or dep_block
                arr = arr_block.get("arrival") or arr_block

                est_dep = dep.get("estimatedTime") or dep.get("scheduledTime") or ""
                est_arr = arr.get("estimatedTime") or arr.get("scheduledTime") or ""

        return {
            "Status": status_text,
            "EstimatedDeparture": est_dep,
            "EstimatedArrival": est_arr,
        }

    except Exception as e:
        return {
            "Status": f"Error: {e}",
            "EstimatedDeparture": "",
            "EstimatedArrival": "",
        }


# ==============================
# Amadeus Settings (Flight Analysis)
# ==============================

AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")

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
    """Roundtrip fare search via Amadeus."""
    try:
        token = get_amadeus_token()
    except Exception as e:
        return {
            "Origin": origin,
            "Destination": destination,
            "DepartureDate": departure_date,
            "ReturnDate": return_date,
            "Price": None,
            "Currency": "USD",
            "Error": f"Auth error: {e}",
        }

    params = {
        "originLocationCode": origin,
        "destinationLocationCode": destination,
        "departureDate": departure_date,
        "returnDate": return_date,
        "adults": 1,
        "currencyCode": "USD",
        "max": 1,
    }
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = requests.get(
            AMADEUS_FLIGHT_OFFERS_URL,
            headers=headers,
            params=params,
            timeout=30,
        )
        if resp.status_code != 200:
            return {
                "Origin": origin,
                "Destination": destination,
                "DepartureDate": departure_date,
                "ReturnDate": return_date,
                "Price": None,
                "Currency": "USD",
                "Error": f"API error {resp.status_code}",
            }

        data = resp.json()
        offers = data.get("data", [])
        if not offers:
            return {
                "Origin": origin,
                "Destination": destination,
                "DepartureDate": departure_date,
                "ReturnDate": return_date,
                "Price": None,
                "Currency": "USD",
                "Error": "No fares found",
            }

        offer = offers[0]
        price_info = offer.get("price", {})
        total = price_info.get("grandTotal")
        currency = price_info.get("currency", "USD")

        return {
            "Origin": origin,
            "Destination": destination,
            "DepartureDate": departure_date,
            "ReturnDate": return_date,
            "Price": total,
            "Currency": currency,
            "Error": "",
        }

    except Exception as e:
        return {
            "Origin": origin,
            "Destination": destination,
            "DepartureDate": departure_date,
            "ReturnDate": return_date,
            "Price": None,
            "Currency": "USD",
            "Error": f"Request error: {e}",
        }


# ==============================
# HOME PAGE
# ==============================

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


# ==============================
# FLIGHT STATUS PAGE
# ==============================

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
        # If a file is present, treat as Excel upload
        if "file" in request.files and request.files["file"].filename:
            file = request.files["file"]

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
                    return render_template(
                        "flight_status.html",
                        company=company,
                        flight_info=None,
                        uploaded_results=None,
                    )

                today_str = datetime.now().strftime("%Y-%m-%d")
                results = []

                for _, row in df.iterrows():
                    airline = str(row["airline"]).strip().upper()
                    flight_number = str(row["flightnumber"]).strip()
                    dep = str(row["departure"]).strip().upper()
                    arr = str(row["arrival"]).strip().upper()

                    details = fetch_status_and_times(airline, flight_number, today_str)

                    results.append(
                        {
                            "Airline": airline,
                            "FlightNumber": flight_number,
                            "From": dep,
                            "To": arr,
                            "Status": details.get("Status", "Unknown"),
                            "EstimatedDeparture": details.get("EstimatedDeparture", ""),
                            "EstimatedArrival": details.get("EstimatedArrival", ""),
                        }
                    )

                uploaded_results = results
                last_results = results  # OPTION B: whatever is displayed is what gets downloaded

            except Exception as e:
                flash(f"Error processing file: {e}")
                return render_template(
                    "flight_status.html",
                    company=company,
                    flight_info=None,
                    uploaded_results=None,
                )

        # Otherwise, handle manual lookup
        else:
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

            today_str = datetime.now().strftime("%Y-%m-%d")
            details = fetch_status_and_times(airline, flight_number, today_str)

            flight_info = {
                "Airline": airline,
                "FlightNumber": flight_number,
                "From": departure,
                "To": arrival,
                "Status": details.get("Status", "Unknown"),
                "EstimatedDeparture": details.get("EstimatedDeparture", ""),
                "EstimatedArrival": details.get("EstimatedArrival", ""),
            }

            last_results = [flight_info]

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results,
    )


# ==============================
# DOWNLOAD STATUS RESULTS (Option B)
# ==============================

@app.route("/download-excel")
def download_excel():
    """Download whichever results were shown last (manual or upload)."""
    global last_results

    if not last_results:
        flash("No results to download yet.")
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


# ==============================
# FLIGHT ANALYSIS (Amadeus)
# ==============================

@app.route("/flight-analysis", methods=["GET", "POST"])
def flight_analysis():
    global last_analysis_results

    company = (request.args.get("company") or request.form.get("company") or "").strip().lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    analysis_results = None

    if request.method == "POST":
        # use getlist() to collect repeated fields
        origins = [o.strip().upper() for o in request.form.getlist("origins") if o.strip()]
        destinations = [d.strip().upper() for d in request.form.getlist("destinations") if d.strip()]
        outbound_date = request.form.get("outbound_date", "").strip()
        return_date = request.form.get("return_date", "").strip()

        if not origins or not destinations:
            flash("Please enter at least one origin and one destination.")
            return render_template("flight_analysis.html", company=company)

        if not outbound_date or not return_date:
            flash("Please select outbound and return dates.")
            return render_template("flight_analysis.html", company=company)

        results = []
        for origin in origins:
            for dest in destinations:
                results.append(
                    search_lowest_fare_amadeus(origin, dest, outbound_date, return_date)
                )

        analysis_results = results
        last_analysis_results = results

    return render_template(
        "flight_analysis.html",
        company=company,
        analysis_results=analysis_results,
    )


@app.route("/download-analysis")
def download_analysis_excel():
    global last_analysis_results

    if not last_analysis_results:
        flash("No analysis results to download yet.")
        return redirect(url_for("home"))

    df = pd.DataFrame(last_analysis_results)
    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(
        output,
        download_name="flight_analysis_results.xlsx",
        as_attachment=True,
    )


if __name__ == "__main__":
    # For local testing
    app.run(debug=True)

