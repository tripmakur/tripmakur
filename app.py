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
app.secret_key = os.getenv("FLASK_SECRET_KEY", "default-secret")

# ============================================================
# Allowed Companies
# ============================================================

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


def is_company_allowed(name: str) -> bool:
    if not ALLOWED_COMPANIES:
        return True
    if not name:
        return False
    return name.lower() in ALLOWED_COMPANIES


# ============================================================
# FlightAPI.io (Flight Status)
# ============================================================

FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY")


def fetch_status_flightapi(airline, number, date_yyyymmdd):
    """
    Calls FlightAPI.io and extracts:
    - status
    - estimated times only if delayed
    """

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={number}&name={airline.lower()}&date={date_yyyymmdd}"
    )

    try:
        resp = requests.get(url, timeout=20)

        if resp.status_code != 200:
            return {
                "Status": f"API Error ({resp.status_code})",
                "EstimatedDeparture": None,
                "EstimatedArrival": None,
            }

        data = resp.json()

        flights = data.get("flights", [])
        if not flights:
            return {
                "Status": "Not Found",
                "EstimatedDeparture": None,
                "EstimatedArrival": None,
            }

        # Choose the segment matching the FROM airport if possible
        chosen_flight = flights[0]

        status_raw = (
            chosen_flight.get("displayStatus")
            or chosen_flight.get("status")
            or "Unknown"
        )

        # Normalize string status
        status = str(status_raw).strip()

        dep = chosen_flight.get("departureTime")
        arr = chosen_flight.get("arrivalTime")

        # Only include estimated times if delayed
        if status.lower() == "delayed":
            return {
                "Status": status,
                "EstimatedDeparture": dep,
                "EstimatedArrival": arr,
            }

        return {
            "Status": status,
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }

    except Exception as e:
        return {
            "Status": f"Error: {e}",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }


# ============================================================
# Amadeus API (Flight Analysis)
# ============================================================

AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")

AM_AUTH = "https://test.api.amadeus.com/v1/security/oauth2/token"
AM_FLIGHT_OFFERS = "https://test.api.amadeus.com/v2/shopping/flight-offers"

amadeus_token = None
amadeus_expiry = 0


def amadeus_get_token():
    global amadeus_token, amadeus_expiry

    now = time.time()
    if amadeus_token and now < amadeus_expiry:
        return amadeus_token

    resp = requests.post(
        AM_AUTH,
        data={
            "grant_type": "client_credentials",
            "client_id": AMADEUS_CLIENT_ID,
            "client_secret": AMADEUS_CLIENT_SECRET,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )

    if resp.status_code != 200:
        raise RuntimeError(f"Amadeus Auth Failed: {resp.text}")

    data = resp.json()
    amadeus_token = data["access_token"]
    amadeus_expiry = now + data.get("expires_in", 1800) - 60
    return amadeus_token


def search_lowest_fare(origin, destination, depart, ret):
    """Amadeus roundtrip fare lookup"""

    try:
        token = amadeus_get_token()
    except Exception as e:
        return {"Error": f"Auth Error: {e}"}

    params = {
        "originLocationCode": origin,
        "destinationLocationCode": destination,
        "departureDate": depart,
        "returnDate": ret,
        "adults": 1,
        "currencyCode": "USD",
        "max": 1,
    }

    resp = requests.get(
        AM_FLIGHT_OFFERS,
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )

    if resp.status_code != 200:
        return {"Error": f"API Error {resp.status_code}"}

    data = resp.json()
    offers = data.get("data", [])

    if not offers:
        return {"Error": "No fares found"}

    price = offers[0].get("price", {})
    return {
        "Price": price.get("grandTotal"),
        "Currency": price.get("currency", "USD"),
        "Error": "",
    }


# ============================================================
# HOME PAGE
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
# FLIGHT STATUS (Manual + Upload)
# ============================================================

@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    company = request.args.get("company", "").lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    flight_info = None
    uploaded_results = None

    # =========================
    # FILE UPLOAD
    # =========================
    if request.method == "POST" and "file" in request.files:
        file = request.files["file"]

        if not file.filename:
            flash("No file selected.")
            return redirect(url_for("flight_status", company=company))

        df = pd.read_excel(file)

        df.columns = df.columns.str.lower().str.strip()
        required = ["airline", "flightnumber", "departure", "arrival"]

        for r in required:
            if r not in df.columns:
                flash(f"Missing required column: {r}")
                return redirect(url_for("flight_status", company=company))

        uploaded_results = []
        today = datetime.utcnow().strftime("%Y%m%d")

        for _, row in df.iterrows():
            airline = str(row["airline"]).strip()
            num = str(row["flightnumber"]).strip()
            dep = str(row["departure"]).strip()
            arr = str(row["arrival"]).strip()

            result = fetch_status_flightapi(airline, num, today)

            uploaded_results.append({
                "Airline": airline,
                "FlightNumber": num,
                "From": dep,
                "To": arr,
                "Status": result["Status"],
                "EstimatedDeparture": result["EstimatedDeparture"],
                "EstimatedArrival": result["EstimatedArrival"],
            })

        return render_template(
            "flight_status.html",
            company=company,
            flight_info=None,
            uploaded_results=uploaded_results,
        )

    # =========================
    # MANUAL LOOKUP
    # =========================
    if request.method == "POST":
        airline = request.form.get("airline", "").strip()
        number = request.form.get("flight_number", "").strip()
        dep = request.form.get("departure", "").strip()
        arr = request.form.get("arrival", "").strip()

        if not all([airline, number, dep, arr]):
            flash("All fields are required.")
            return render_template("flight_status.html", company=company)

        today = datetime.utcnow().strftime("%Y%m%d")

        result = fetch_status_flightapi(airline, number, today)

        flight_info = {
            "Airline": airline,
            "FlightNumber": number,
            "From": dep,
            "To": arr,
            "Status": result["Status"],
            "EstimatedDeparture": result["EstimatedDeparture"],
            "EstimatedArrival": result["EstimatedArrival"],
        }

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results,
    )


# ============================================================
# DOWNLOAD STATUS AS EXCEL
# ============================================================

@app.route("/download-status", methods=["POST"])
def download_status():
    results = request.form.get("results_json")

    if not results:
        flash("No results to download.")
        return redirect(url_for("home"))

    data = json.loads(results)
    df = pd.DataFrame(data)

    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(output, as_attachment=True, download_name="flight_status.xlsx")


# ============================================================
# FLIGHT ANALYSIS (Amadeus)
# ============================================================

@app.route("/flight-analysis", methods=["GET", "POST"])
def flight_analysis():
    company = request.args.get("company", "").lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    analysis = None

    if request.method == "POST":
        origins = [o.strip().upper() for o in request.form.getlist("origins") if o.strip()]
        dests = [d.strip().upper() for d in request.form.getlist("destinations") if d.strip()]

        depart = request.form.get("outbound_date", "")
        ret = request.form.get("return_date", "")

        if not origins or not dests:
            flash("Enter at least 1 origin and 1 destination.")
            return render_template("flight_analysis.html", company=company)

        if not depart or not ret:
            flash("Select both dates.")
            return render_template("flight_analysis.html", company=company)

        analysis = []

        for o in origins:
            for d in dests:
                result = search_lowest_fare(o, d, depart, ret)
                analysis.append({
                    "Origin": o,
                    "Destination": d,
                    "Price": result.get("Price"),
                    "Currency": result.get("Currency"),
                    "Error": result.get("Error", ""),
                })

    return render_template("flight_analysis.html", company=company, analysis_results=analysis)


# ============================================================
# RUN APP
# ============================================================

if __name__ == "__main__":
    app.run(debug=True)


