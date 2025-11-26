import os
import json
import time
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file

app = Flask(__name__)
app.secret_key = "your-secret-key"


# ======================================================
# Allowed Companies
# ======================================================
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
last_results = []
last_analysis_results = []


def is_company_allowed(company: str) -> bool:
    if not ALLOWED_COMPANIES:
        return True
    return company.lower() in ALLOWED_COMPANIES


# ======================================================
# Default Timezone Fix (CST)
# ======================================================
LOCAL_TZ = ZoneInfo("America/Chicago")  # CST/CDT


def today_local_date_str():
    """Return today's date based on CST timezone."""
    now = datetime.now(LOCAL_TZ)
    return now.strftime("%Y-%m-%d")


# ======================================================
# FlightAPI.io (Flight Status)
# ======================================================
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY", "69175603253bb1627f7ea9cc")


def parse_estimated(field):
    """Convert '20:15, Nov 26' → same string (clean)."""
    if not field:
        return None
    return field.strip()


def extract_status_fields(api_response):
    """
    Extract normalized fields:
    - Status
    - EstimatedDeparture
    - EstimatedArrival
    """
    if isinstance(api_response, list):
        est_dep = None
        est_arr = None
        status_found = None

        for item in api_response:
            if "departure" in item:
                est_dep = parse_estimated(item["departure"].get("estimatedTime"))
            if "arrival" in item:
                est_arr = parse_estimated(item["arrival"].get("estimatedTime"))
            if "status" in item:
                status_found = item["status"]

        return status_found or "Unknown", est_dep, est_arr

    if isinstance(api_response, dict) and "flights" in api_response:
        flights = api_response["flights"]
        if not flights:
            return "Not Found", None, None

        first = flights[0]
        status = first.get("displayStatus") or first.get("status") or "Unknown"
        return status, None, None

    return "Not Found", None, None


def fetch_status_flightapi(airline: str, flight_number: str, flight_date: str):
    airline_code = airline.upper()
    date_str = flight_date.replace("-", "")

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline_code}&date={date_str}"
    )

    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            return {
                "Status": f"API Error {resp.status_code}",
                "EstimatedDeparture": None,
                "EstimatedArrival": None,
            }

        data = resp.json()
        status, est_dep, est_arr = extract_status_fields(data)

        return {
            "Status": status,
            "EstimatedDeparture": est_dep,
            "EstimatedArrival": est_arr,
        }

    except Exception as e:
        return {
            "Status": f"Error {str(e)}",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }


# ======================================================
# Amadeus API (Flight Analysis)
# ======================================================
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID", "ZGOUVp1fU3EGHoNGEuNgjJDtunQ9GgNe")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET", "4oJvcvPX5qWd0dgh")

AMADEUS_AUTH_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
AMADEUS_FLIGHT_OFFERS_URL = "https://test.api.amadeus.com/v2/shopping/flight-offers"

amadeus_token = None
amadeus_token_expiry = 0


def get_amadeus_token():
    global amadeus_token, amadeus_token_expiry
    now = time.time()

    if amadeus_token and now < amadeus_token_expiry:
        return amadeus_token

    resp = requests.post(
        AMADEUS_AUTH_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": AMADEUS_CLIENT_ID,
            "client_secret": AMADEUS_CLIENT_SECRET,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )

    if resp.status_code != 200:
        raise RuntimeError("Amadeus authentication failed")

    payload = resp.json()
    amadeus_token = payload["access_token"]
    amadeus_token_expiry = now + payload.get("expires_in", 1800) - 60
    return amadeus_token


def search_lowest_fare_amadeus(origin, destination, dep, ret):
    try:
        token = get_amadeus_token()
    except Exception as e:
        return {"Error": f"Auth Error: {e}"}

    resp = requests.get(
        AMADEUS_FLIGHT_OFFERS_URL,
        headers={"Authorization": f"Bearer {token}"},
        params={
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDate": dep,
            "returnDate": ret,
            "adults": 1,
            "currencyCode": "USD",
            "max": 1,
        },
        timeout=30,
    )

    if resp.status_code != 200:
        return {"Error": f"API Error {resp.status_code}"}

    data = resp.json()
    offers = data.get("data", [])

    if not offers:
        return {"Error": "No fares found"}

    price_info = offers[0].get("price", {})
    return {
        "Price": price_info.get("grandTotal"),
        "Currency": price_info.get("currency", "USD"),
        "Error": "",
    }


# ======================================================
# ROUTES
# ======================================================

@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        company = request.form.get("company", "").strip().lower()

        if not company:
            flash("Please enter a company name.")
            return render_template("index.html")

        if not is_company_allowed(company):
            flash("Access denied.")
            return render_template("index.html")

        return redirect(url_for("flight_status", company=company))

    return render_template("index.html")


@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    global last_results

    company = (
        request.args.get("company")
        or request.form.get("company")
        or ""
    ).strip().lower()

    if not is_company_allowed(company):
        flash("Access Denied")
        return redirect(url_for("home"))

    flight_info = None
    uploaded_results = None

    if request.method == "POST":
        airline = request.form.get("airline", "").strip().upper()
        flight_number = request.form.get("flight_number", "").strip()
        dep = request.form.get("departure", "").strip().upper()
        arr = request.form.get("arrival", "").strip().upper()

        if not all([airline, flight_number, dep, arr]):
            flash("All fields are required.")
            return render_template("flight_status.html", company=company)

        today = today_local_date_str()
        status_data = fetch_status_flightapi(airline, flight_number, today)

        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": dep,
            "To": arr,
            "Status": status_data["Status"],
            "EstimatedDeparture": status_data["EstimatedDeparture"],
            "EstimatedArrival": status_data["EstimatedArrival"],
        }

        last_results = [flight_info]

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results,
    )


@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results

    company = request.args.get("company", "").lower()
    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    if "file" not in request.files:
        flash("No file uploaded.")
        return redirect(url_for("flight_status", company=company))

    file = request.files["file"]
    if file.filename == "":
        flash("No selected file.")
        return redirect(url_for("flight_status", company=company))

    try:
        df = (
            pd.read_csv(file)
            if file.filename.endswith(".csv")
            else pd.read_excel(file, sheet_name=0)
        )

        df.columns = df.columns.str.lower().str.strip()

        required = ["airline", "flightnumber", "departure", "arrival"]
        if not all(col in df.columns for col in required):
            flash("Missing required columns.")
            return redirect(url_for("flight_status", company=company))

        today = today_local_date_str()
        results = []

        for _, row in df.iterrows():
            airline = row["airline"].strip().upper()
            flight_number = str(row["flightnumber"]).strip()
            dep = row["departure"].strip().upper()
            arr = row["arrival"].strip().upper()

            status_data = fetch_status_flightapi(airline, flight_number, today)

            results.append({
                "Airline": airline,
                "FlightNumber": flight_number,
                "From": dep,
                "To": arr,
                "Status": status_data["Status"],
                "EstimatedDeparture": status_data["EstimatedDeparture"],
                "EstimatedArrival": status_data["EstimatedArrival"],
            })

        last_results = results

        return render_template(
            "flight_status.html",
            company=company,
            uploaded_results=results,
            flight_info=None,
        )

    except Exception as e:
        flash(f"File error: {e}")
        return redirect(url_for("flight_status", company=company))


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


# ======================================================
# FLIGHT ANALYSIS
# ======================================================

@app.route("/flight-analysis", methods=["GET", "POST"])
def flight_analysis():
    global last_analysis_results

    company = (
        request.args.get("company")
        or request.form.get("company")
        or ""
    ).lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    analysis_results = None

    if request.method == "POST":
        origins = [o.strip().upper() for o in request.form.getlist("origins") if o.strip()]
        dests = [d.strip().upper() for d in request.form.getlist("destinations") if d.strip()]
        dep_date = request.form.get("outbound_date", "")
        ret_date = request.form.get("return_date", "")

        if not origins or not dests:
            flash("Enter at least 1 origin and 1 destination.")
            return render_template("flight_analysis.html", company=company)

        analysis_results = []
        for o in origins:
            for d in dests:
                result = search_lowest_fare_amadeus(o, d, dep_date, ret_date)
                analysis_results.append({
                    "Origin": o,
                    "Destination": d,
                    "Price": result.get("Price"),
                    "Currency": result.get("Currency", "USD"),
                    "Error": result.get("Error"),
                })

        last_analysis_results = analysis_results

    return render_template(
        "flight_analysis.html",
        company=company,
        analysis_results=analysis_results,
    )


@app.route("/download-analysis")
def download_analysis_excel():
    if not last_analysis_results:
        flash("No analysis results to download.")
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
    app.run(debug=True)
