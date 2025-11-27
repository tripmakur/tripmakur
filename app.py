import os
import json
import time
from datetime import datetime
from io import BytesIO

import pandas as pd
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "your-secret-key")

# ------------------------------------------
# Allowed Companies
# ------------------------------------------
ALLOWED_COMPANIES_FILE = "allowed_companies.json"


def load_allowed_companies():
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        try:
            with open(ALLOWED_COMPANIES_FILE, "r") as f:
                companies = json.load(f)
                return [c.strip().lower() for c in companies]
        except:
            return []
    return []


ALLOWED_COMPANIES = load_allowed_companies()


def is_company_allowed(company):
    if not ALLOWED_COMPANIES:
        return True
    if not company:
        return False
    return company.lower() in ALLOWED_COMPANIES


# ------------------------------------------
# FlightAPI.io Keys
# ------------------------------------------

FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY")


# ------------------------------------------
# Fetch Flight Status (Corrected Parser)
# ------------------------------------------
def fetch_status_flightapi(airline: str, flight_number: str, flight_date: str):
    """
    Correct parser for FlightAPI.io real structure.
    Returns dict with:
      - Status
      - EstimatedDeparture (only if delayed)
      - EstimatedArrival   (only if delayed)
    """

    airline = airline.strip().upper()
    flight_number = str(flight_number).strip()
    date_clean = flight_date.replace("-", "")

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline}&date={date_clean}"
    )

    try:
        resp = requests.get(url, timeout=15)

        if resp.status_code != 200:
            return {
                "Status": f"API Error {resp.status_code}",
                "EstimatedDeparture": None,
                "EstimatedArrival": None,
            }

        data = resp.json()

        flights = data.get("flights")
        if not flights:
            return {
                "Status": "Not Found",
                "EstimatedDeparture": None,
                "EstimatedArrival": None,
            }

        flight = flights[0]

        status = flight.get("displayStatus") or "Unknown"
        dep_time = flight.get("departureTime")
        arr_time = flight.get("arrivalTime")

        # Only return times if delayed
        if "delayed" in status.lower():
            return {
                "Status": status,
                "EstimatedDeparture": dep_time,
                "EstimatedArrival": arr_time,
            }

        return {
            "Status": status,
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }

    except Exception as e:
        return {
            "Status": f"Error: {str(e)}",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }


# ------------------------------------------
# Home Page
# ------------------------------------------
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


# ------------------------------------------
# Flight Status Page
# ------------------------------------------
@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    company = (
        request.args.get("company")
        or request.form.get("company")
        or ""
    ).strip().lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    flight_info = None
    uploaded_results = None

    # ------------------------------
    # MANUAL LOOKUP
    # ------------------------------
    if request.method == "POST" and "file" not in request.files:
        airline = request.form.get("airline", "").strip().upper()
        flight_number = request.form.get("flight_number", "").strip()
        departure = request.form.get("departure", "").strip().upper()
        arrival = request.form.get("arrival", "").strip().upper()

        if not all([airline, flight_number, departure, arrival]):
            flash("All fields are required.")
            return render_template("flight_status.html", company=company)

        today = datetime.today().strftime("%Y-%m-%d")

        result = fetch_status_flightapi(airline, flight_number, today)

        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": departure,
            "To": arrival,
            "Status": result["Status"],
            "EstimatedDeparture": result["EstimatedDeparture"],
            "EstimatedArrival": result["EstimatedArrival"],
        }

    # ------------------------------
    # EXCEL UPLOAD
    # ------------------------------
    if request.method == "POST" and "file" in request.files:

        file = request.files["file"]

        if not file.filename:
            flash("No file selected.")
            return render_template("flight_status.html", company=company)

        try:
            if file.filename.lower().endswith(".csv"):
                df = pd.read_csv(file)
            else:
                df = pd.read_excel(file)

            df.columns = df.columns.str.lower().str.strip()
            required = ["airline", "flightnumber", "departure", "arrival"]

            missing = [c for c in required if c not in df.columns]
            if missing:
                flash(f"Missing columns: {missing}")
                return render_template("flight_status.html", company=company)

            today = datetime.today().strftime("%Y-%m-%d")
            uploaded_results = []

            for _, row in df.iterrows():
                airline = str(row["airline"]).strip().upper()
                flight_number = str(row["flightnumber"]).strip()
                dep = str(row["departure"]).strip().upper()
                arr = str(row["arrival"]).strip().upper()

                result = fetch_status_flightapi(airline, flight_number, today)

                uploaded_results.append({
                    "Airline": airline,
                    "FlightNumber": flight_number,
                    "From": dep,
                    "To": arr,
                    "Status": result["Status"],
                    "EstimatedDeparture": result["EstimatedDeparture"],
                    "EstimatedArrival": result["EstimatedArrival"],
                })

        except Exception as e:
            flash(f"Error processing file: {e}")
            return render_template("flight_status.html", company=company)

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results,
    )


# ------------------------------------------
# Download Excel
# ------------------------------------------
@app.route("/download_excel")
def download_excel():
    # You can wire this later when needed
    flash("Download currently disabled.")
    return redirect(url_for("home"))


# ------------------------------------------
# Flight Analysis (Enabled)
# ------------------------------------------
@app.route("/flight-analysis", methods=["GET", "POST"])
def flight_analysis():
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
        origins = [o.strip().upper() for o in request.form.getlist("origins") if o.strip()]
        destinations = [d.strip().upper() for d in request.form.getlist("destinations") if d.strip()]
        outbound_date = request.form.get("outbound_date", "").strip()
        return_date = request.form.get("return_date", "").strip()

        if not origins or not destinations:
            flash("Please enter at least one origin and one destination.")
            return render_template("flight_analysis.html", company=company)

        if not outbound_date or not return_date:
            flash("Please enter both outbound and return dates.")
            return render_template("flight_analysis.html", company=company)

        analysis_results = []
        for o in origins:
            for d in destinations:
                analysis_results.append({
                    "Origin": o,
                    "Destination": d,
                    "Price": "N/A",
                    "Currency": "USD",
                    "Error": "Amadeus disabled for safety."
                })

    return render_template(
        "flight_analysis.html",
        company=company,
        analysis_results=analysis_results
    )


# ------------------------------------------
# Start Server
# ------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)







