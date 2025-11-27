import os
import json
import time
from datetime import datetime
from io import BytesIO

import pandas as pd
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "default-secret-key")

# ============================================================
# Allowed Companies
# ============================================================
ALLOWED_COMPANIES_FILE = "allowed_companies.json"


def load_allowed_companies():
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        try:
            with open(ALLOWED_COMPANIES_FILE, "r") as f:
                companies = json.load(f)
                return [str(c).strip().lower() for c in companies]
        except:
            return []
    return []


ALLOWED_COMPANIES = load_allowed_companies()


def is_company_allowed(company: str) -> bool:
    if not ALLOWED_COMPANIES:
        return True
    return company.lower() in ALLOWED_COMPANIES


# ============================================================
# FlightAPI.io — Flight Status
# ============================================================
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY")


def parse_flight_status(data, airline, flight_number):
    """
    Parses FlightAPI.io response safely and returns:
        status,
        est_departure (or None),
        est_arrival (or None)
    """

    if not isinstance(data, dict):
        return "Not Found", None, None

    flights = data.get("flights", [])
    if not flights:
        return "Not Found", None, None

    # FIRST matching flight record
    flight = flights[0]

    status = flight.get("displayStatus") or "Unknown"
    dep = flight.get("departureTime")
    arr = flight.get("arrivalTime")

    # Convert times like "09:29, Nov 26" to something nice
    def parse_time(t):
        try:
            return t
        except:
            return None

    parsed_dep = parse_time(dep)
    parsed_arr = parse_time(arr)

    return status, parsed_dep, parsed_arr


def fetch_status_flightapi(airline: str, flight_number: str) -> dict:
    airline = airline.upper()
    today = datetime.now().strftime("%Y%m%d")  # always today

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline}&date={today}"
    )

    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            return {
                "Status": f"API {resp.status_code}",
                "EstimatedDeparture": None,
                "EstimatedArrival": None,
            }

        data = resp.json()
        status, est_dep, est_arr = parse_flight_status(data, airline, flight_number)

        return {
            "Status": status,
            "EstimatedDeparture": est_dep,
            "EstimatedArrival": est_arr,
        }

    except Exception as e:
        return {
            "Status": f"Error: {e}",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }


# ============================================================
# Amadeus Pricing API
# ============================================================
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")

AMADEUS_AUTH_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
AMADEUS_OFFER_URL = "https://test.api.amadeus.com/v2/shopping/flight-offers"

amadeus_token = None
amadeus_token_expiry = 0


def get_amadeus_token():
    global amadeus_token, amadeus_token_expiry

    now = time.time()
    if amadeus_token and now < amadeus_token_expiry:
        return amadeus_token

    payload = {
        "grant_type": "client_credentials",
        "client_id": AMADEUS_CLIENT_ID,
        "client_secret": AMADEUS_CLIENT_SECRET,
    }

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    resp = requests.post(AMADEUS_AUTH_URL, data=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError("Amadeus auth failed")

    data = resp.json()
    amadeus_token = data["access_token"]
    amadeus_token_expiry = now + data.get("expires_in", 1800) - 60

    return amadeus_token


def search_lowest_fare_amadeus(origin, dest, dep_date, ret_date):
    try:
        token = get_amadeus_token()
    except Exception as e:
        return {"Origin": origin, "Destination": dest, "Error": f"Auth: {e}"}

    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "originLocationCode": origin,
        "destinationLocationCode": dest,
        "departureDate": dep_date,
        "returnDate": ret_date,
        "adults": 1,
        "currencyCode": "USD",
        "max": 1,
    }

    try:
        resp = requests.get(AMADEUS_OFFER_URL, headers=headers, params=params)
        if resp.status_code != 200:
            return {"Origin": origin, "Destination": dest, "Error": "API error"}

        offers = resp.json().get("data", [])
        if not offers:
            return {"Origin": origin, "Destination": dest, "Error": "No fares found"}

        offer = offers[0]
        price = offer["price"]["grandTotal"]
        currency = offer["price"].get("currency", "USD")

        return {
            "Origin": origin,
            "Destination": dest,
            "Price": price,
            "Currency": currency,
            "Error": "",
        }

    except Exception as e:
        return {"Origin": origin, "Destination": dest, "Error": str(e)}


# ============================================================
# Index Page
# ============================================================
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


# ============================================================
# Flight Status Page
# ============================================================
@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    company = (request.args.get("company") or "").lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    flight_info = None
    uploaded_results = None

    if request.method == "POST":
        if "file" not in request.files:  # manual lookup
            airline = request.form.get("airline", "").upper()
            number = request.form.get("flight_number", "")
            dep = request.form.get("departure", "").upper()
            arr = request.form.get("arrival", "").upper()

            status_data = fetch_status_flightapi(airline, number)

            flight_info = {
                "Airline": airline,
                "FlightNumber": number,
                "From": dep,
                "To": arr,
                "Status": status_data["Status"],
                "EstimatedDeparture": status_data["EstimatedDeparture"],
                "EstimatedArrival": status_data["EstimatedArrival"],
            }

        else:  # Excel upload
            file = request.files["file"]
            if file.filename == "":
                flash("No file selected.")
                return redirect(url_for("flight_status", company=company))

            df = pd.read_excel(file)
            df.columns = df.columns.str.lower()

            results = []
            for _, row in df.iterrows():
                airline = str(row["airline"]).upper()
                number = str(row["flightnumber"])
                dep = str(row["departure"]).upper()
                arr = str(row["arrival"]).upper()

                statusData = fetch_status_flightapi(airline, number)

                results.append(
                    {
                        "Airline": airline,
                        "FlightNumber": number,
                        "From": dep,
                        "To": arr,
                        "Status": statusData["Status"],
                        "EstimatedDeparture": statusData["EstimatedDeparture"],
                        "EstimatedArrival": statusData["EstimatedArrival"],
                    }
                )

            uploaded_results = results

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results,
    )


# ============================================================
# Flight Analysis — Amadeus Pricing
# ============================================================
@app.route("/flight-analysis", methods=["GET", "POST"])
def flight_analysis():
    company = (request.args.get("company") or "").lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    results = None

    if request.method == "POST":
        origins = [o.strip().upper() for o in request.form.getlist("origins") if o.strip()]
        destinations = [d.strip().upper() for d in request.form.getlist("destinations") if d.strip()]
        dep_date = request.form.get("outbound_date", "")
        ret_date = request.form.get("return_date", "")

        if not origins or not destinations:
            flash("Please enter at least 1 origin and 1 destination.")
            return render_template("flight_analysis.html", company=company)

        temp = []
        for o in origins:
            for d in destinations:
                temp.append(search_lowest_fare_amadeus(o, d, dep_date, ret_date))

        results = temp

    return render_template(
        "flight_analysis.html",
        company=company,
        analysis_results=results,
    )


# ============================================================
# Run App
# ============================================================
if __name__ == "__main__":
    app.run(debug=True)





