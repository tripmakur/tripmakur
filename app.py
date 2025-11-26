import os
import json
import time
from datetime import datetime
from io import BytesIO

import pandas as pd
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "fallback-secret")

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
    if not ALLOWED_COMPANIES:
        return True
    if not company:
        return False
    return company.lower() in ALLOWED_COMPANIES


# ================================
# FlightAPI.io (Flight Status)
# ================================
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY")


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

        # Case 1 — list format: [ {departure...}, {arrival...}, {status...} ]
        if isinstance(data, list):
            status = None
            est_dep = None
            est_arr = None

            for block in data:
                if "status" in block:
                    status = block.get("status")
                if "departure" in block:
                    est_dep = block["departure"].get("estimated")
                if "arrival" in block:
                    est_arr = block["arrival"].get("estimated")

            return {
                "Status": status or "Unknown",
                "EstimatedDeparture": est_dep,
                "EstimatedArrival": est_arr,
            }

        # Case 2 — dict format
        if isinstance(data, dict) and "flights" in data:
            flights = data.get("flights") or []
            if not flights:
                return {
                    "Status": "Not Found",
                    "EstimatedDeparture": None,
                    "EstimatedArrival": None,
                }

            f = flights[0]

            status_code = f.get("displayStatus") or f.get("status")
            if isinstance(status_code, int):
                status_map = {
                    1: "Scheduled",
                    2: "Arrived",
                    3: "Departed",
                    4: "Delayed",
                    5: "Cancelled",
                }
                status = status_map.get(status_code, "Unknown")
            else:
                status = status_code

            est_dep = f.get("departure", {}).get("estimated")
            est_arr = f.get("arrival", {}).get("estimated")

            return {
                "Status": status or "Unknown",
                "EstimatedDeparture": est_dep,
                "EstimatedArrival": est_arr,
            }

        return {
            "Status": "Not Found",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }

    except Exception as e:
        return {
            "Status": f"Error: {str(e)}",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }


# ================================
# Amadeus (Flight Analysis)
# ================================
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")

AMADEUS_AUTH_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
AMADEUS_FLIGHT_OFFERS_URL = "https://test.api.amadeus.com/v2/shopping/flight-offers"

amadeus_token = None
amadeus_expiry = 0


def get_amadeus_token():
    global amadeus_token, amadeus_expiry

    now = time.time()
    if amadeus_token and now < amadeus_expiry:
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
    amadeus_expiry = now + payload.get("expires_in", 1800) - 60

    return amadeus_token


def search_lowest_fare_amadeus(origin, destination, departure_date, return_date):
    try:
        token = get_amadeus_token()
    except Exception as e:
        return {"Origin": origin, "Destination": destination, "Error": f"Auth error: {e}"}

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
        resp = requests.get(AMADEUS_FLIGHT_OFFERS_URL, headers=headers, params=params, timeout=30)
        if resp.status_code != 200:
            return {"Origin": origin, "Destination": destination, "Error": f"API error {resp.status_code}"}

        data = resp.json()
        offers = data.get("data", [])
        if not offers:
            return {"Origin": origin, "Destination": destination, "Error": "No fares found"}

        offer = offers[0]
        total = offer.get("price", {}).get("grandTotal")
        currency = offer.get("price", {}).get("currency", "USD")

        return {
            "Origin": origin,
            "Destination": destination,
            "Price": total,
            "Currency": currency,
            "Error": "",
        }

    except Exception as e:
        return {"Origin": origin, "Destination": destination, "Error": f"Request error: {e}"}


# ================================
# HOME
# ================================
@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        company = request.form.get("company", "").strip().lower()

        if not is_company_allowed(company):
            flash("Access denied.")
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

        today = datetime.utcnow().strftime("%Y-%m-%d")

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

        last_results = [flight_info]

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results,
    )


# ================================
# FLIGHT ANALYSIS PAGE
# ================================
@app.route("/flight-analysis", methods=["GET", "POST"])
def flight_analysis():
    global last_analysis_results

    company = (request.args.get("company") or request.form.get("company") or "").strip().lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    analysis_results = None

    if request.method == "POST":
        origins = [o.strip().upper() for o in request.form.getlist("origins") if o.strip()]
        destinations = [d.strip().upper() for d in request.form.getlist("destinations") if d.strip()]
        outbound_date = request.form.get("outbound_date", "")
        return_date = request.form.get("return_date", "")

        if not origins or not destinations:
            flash("Please enter at least one origin and one destination.")
            return render_template("flight_analysis.html", company=company)

        analysis_results = []
        for o in origins:
            for d in destinations:
                res = search_lowest_fare_amadeus(o, d, outbound_date, return_date)
                analysis_results.append(res)

        last_analysis_results = analysis_results

    return render_template("flight_analysis.html", company=company, analysis_results=analysis_results)


# ================================
# DOWNLOADS
# ================================
@app.route("/download")
def download_excel():
    if not last_results:
        flash("No results available.")
        return redirect(url_for("home"))

    df = pd.DataFrame(last_results)
    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(output, as_attachment=True, download_name="flight_status.xlsx")


@app.route("/download-analysis")
def download_analysis():
    if not last_analysis_results:
        flash("No results available.")
        return redirect(url_for("home"))

    df = pd.DataFrame(last_analysis_results)
    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(output, as_attachment=True, download_name="flight_analysis.xlsx")


# ================================
# START
# ================================
if __name__ == "__main__":
    app.run(debug=True)

