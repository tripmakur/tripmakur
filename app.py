import os
import json
from datetime import datetime
from io import BytesIO

import pandas as pd
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file

app = Flask(__name__)
app.secret_key = "your-secret-key"

# ==================================================================
# Allowed Companies Loader
# ==================================================================
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

def is_company_allowed(company: str) -> bool:
    if not ALLOWED_COMPANIES:   # allow all if empty
        return True
    return company.lower() in ALLOWED_COMPANIES


# ==================================================================
# FlightAPI.io — Flight Status Lookup
# ==================================================================

FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY", "69175603253bb1627f7ea9cc")

def fetch_status_flightapi(airline: str, flight_number: str, flight_date: str) -> dict:
    """
    Returns:
    {
        "Status": "Delayed",
        "EstimatedDeparture": "14:40, Nov 26",
        "EstimatedArrival": "17:06, Nov 26"
    }
    """

    airline_code = airline.upper()
    date_str = flight_date.replace("-", "")

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline_code}&date={date_str}"
    )

    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            return {"Status": f"API Error {resp.status_code}", "EstimatedDeparture": None, "EstimatedArrival": None}

        data = resp.json()
        flights = data.get("flights")

        # If we have valid flights returned
        if flights and isinstance(flights, list):
            first = flights[0]

            status = first.get("displayStatus", "Unknown")
            dep = first.get("departureTime")
            arr = first.get("arrivalTime")

            # Only show estimated times if delayed
            show_times = status.lower() == "delayed"

            return {
                "Status": status,
                "EstimatedDeparture": dep if show_times else None,
                "EstimatedArrival": arr if show_times else None
            }

        return {"Status": "Not Found", "EstimatedDeparture": None, "EstimatedArrival": None}

    except Exception as e:
        return {"Status": f"Error: {str(e)}", "EstimatedDeparture": None, "EstimatedArrival": None}


# ==================================================================
# HOME PAGE
# ==================================================================
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


# ==================================================================
# FLIGHT STATUS PAGE
# ==================================================================
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

    # Manual lookup
    if request.method == "POST" and "airline" in request.form:
        airline = request.form.get("airline", "").strip().upper()
        flight_number = request.form.get("flight_number", "").strip()

        if not airline or not flight_number:
            flash("Airline and Flight Number required.")
            return render_template("flight_status.html", company=company)

        today = datetime.today().strftime("%Y-%m-%d")
        status_data = fetch_status_flightapi(airline, flight_number, today)

        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "Status": status_data["Status"],
            "EstimatedDeparture": status_data["EstimatedDeparture"],
            "EstimatedArrival": status_data["EstimatedArrival"],
        }

        return render_template("flight_status.html", company=company, flight_info=flight_info)

    return render_template("flight_status.html", company=company)





