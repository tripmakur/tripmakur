import os
import json
import time
from datetime import datetime
from io import BytesIO
import pytz

import pandas as pd
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this")

# ================================
# Allowed Companies
# ================================
ALLOWED_COMPANIES_FILE = "allowed_companies.json"


def load_allowed_companies():
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        try:
            with open(ALLOWED_COMPANIES_FILE, "r") as f:
                companies = json.load(f)
                return [c.lower().strip() for c in companies]
        except:
            return []
    return []


ALLOWED_COMPANIES = load_allowed_companies()

# ================================
# Environment Variables (SECURED)
# ================================
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY")
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")

if not FLIGHTAPI_KEY:
    raise RuntimeError("FLIGHTAPI_KEY not configured in environment variables.")

if not AMADEUS_CLIENT_ID or not AMADEUS_CLIENT_SECRET:
    raise RuntimeError("Amadeus API credentials missing in environment variables.")


# ================================
# Timezone Helper (CST default)
# ================================
def get_local_today():
    user_tz = os.getenv("DEFAULT_TZ", "America/Chicago")  # CST default
    tz = pytz.timezone(user_tz)
    now = datetime.now(tz)
    return now.strftime("%Y-%m-%d")


# ================================
# FLIGHT STATUS — FlightAPI.io
# ================================
def fetch_status_flightapi(airline, flight_number, flight_date):
    airline = airline.upper()
    date_str = flight_date.replace("-", "")

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline}&date={date_str}"
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

        if isinstance(data, list):
            status = next(
                (blk.get("status") for blk in data if "status" in blk), "Unknown"
            )

            dep = next(
                (blk.get("estimatedTime") for blk in data if "departure" in blk), None
            )

            arr = next(
                (blk.get("estimatedTime") for blk in data if "arrival" in blk), None
            )

            return {
                "Status": status,
                "EstimatedDeparture": dep,
                "EstimatedArrival": arr,
            }

        if isinstance(data, dict) and "flights" in data:
            f = data["flights"][0]
            status = f.get("displayStatus") or f.get("status") or "Unknown"

            dep = f.get("estimatedDepartureTime")
            arr = f.get("estimatedArrivalTime")

            return {
                "Status": status,
                "EstimatedDeparture": dep,
                "EstimatedArrival": arr,
            }

        return {"Status": "Not Found", "EstimatedDeparture": None, "EstimatedArrival": None}

    except Exception:
        return {"Status": "Error", "EstimatedDeparture": None, "EstimatedArrival": None}


# ================================
# HOME
# ================================
@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        company = request.form.get("company", "").lower().strip()

        if not company:
            flash("Please enter a company name.")
            return render_template("index.html")

        if ALLOWED_COMPANIES and company not in ALLOWED_COMPANIES:
            flash("Access denied.")
            return render_template("index.html")

        return redirect(url_for("flight_status", company=company))

    return render_template("index.html")


# ================================
# FLIGHT STATUS PAGE
# ================================
@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    company = request.args.get("company", "").lower()
    if ALLOWED_COMPANIES and company not in ALLOWED_COMPANIES:
        flash("Access denied.")
        return redirect(url_for("home"))

    manual_result = None
    uploaded_results = None

    if request.method == "POST":
        airline = request.form.get("airline", "").upper()
        flight_number = request.form.get("flight_number", "")
        dep = request.form.get("departure", "").upper()
        arr = request.form.get("arrival", "").upper()

        today = get_local_today()
        status_info = fetch_status_flightapi(airline, flight_number, today)

        manual_result = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": dep,
            "To": arr,
            **status_info,
        }

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=manual_result,
        uploaded_results=uploaded_results,
    )


# ================================
# UPLOAD (Excel)
# ================================
@app.route("/upload", methods=["POST"])
def upload():
    company = request.args.get("company", "").lower()
    if ALLOWED_COMPANIES and company not in ALLOWED_COMPANIES:
        flash("Access denied.")
        return redirect(url_for("home"))

    file = request.files.get("file")
    if not file:
        flash("No file uploaded.")
        return redirect(url_for("flight_status", company=company))

    df = pd.read_excel(file)
    df.columns = df.columns.str.lower().str.strip()

    today = get_local_today()
    results = []

    for _, row in df.iterrows():
        airline = row.get("airline", "").upper()
        flight = str(row.get("flightnumber", ""))
        dep = row.get("departure", "").upper()
        arr = row.get("arrival", "").upper()

        info = fetch_status_flightapi(airline, flight, today)

        results.append({
            "Airline": airline,
            "FlightNumber": flight,
            "From": dep,
            "To": arr,
            **info
        })

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=None,
        uploaded_results=results,
    )


# ================================
# FLIGHT ANALYSIS
# ================================
# (safe version unchanged for now)
from flight_analysis_logic import register_flight_analysis_routes
register_flight_analysis_routes(app)


if __name__ == "__main__":
    app.run(debug=False)
