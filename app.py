import os
import json
import time
from datetime import datetime, timedelta
from io import BytesIO

import pandas as pd
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "CHANGE_ME_IN_RENDER")

# ==========================================
#  ALLOWED COMPANIES
# ==========================================

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
last_results = []
last_analysis_results = []


def is_company_allowed(company: str) -> bool:
    if not ALLOWED_COMPANIES:
        return True
    return company.lower() in ALLOWED_COMPANIES


# ==========================================
#  FLIGHTAPI.IO — FLIGHT STATUS
# ==========================================

FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY")   # must be set in Render


def clean_date_for_api():
    """Returns today’s date formatted for FlightAPI.io (YYYYMMDD)."""
    today = datetime.utcnow() - timedelta(hours=6)  # shift to approx US Central time
    return today.strftime("%Y%m%d")


def fetch_status_flightapi(airline: str, flight_number: str) -> dict:
    """
    Contact FlightAPI.io and return:
    {
        "Status": "...",
        "EstimatedDeparture": "...",
        "EstimatedArrival": "..."
    }
    """

    if not FLIGHTAPI_KEY:
        return {"Status": "API Key Missing", "EstimatedDeparture": None, "EstimatedArrival": None}

    airline = airline.upper()
    date_str = clean_date_for_api()

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline}&date={date_str}"
    )

    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            return {"Status": f"API Error {resp.status_code}", "EstimatedDeparture": None, "EstimatedArrival": None}

        data = resp.json()

        # Case 1: Simple list response
        if isinstance(data, list):
            status = None
            est_dep, est_arr = None, None
            for block in data:
                if "status" in block:
                    status = block["status"]
                if "departure" in block:
                    est_dep = block["departure"].get("estimatedTime")
                if "arrival" in block:
                    est_arr = block["arrival"].get("estimatedTime")
            return {
                "Status": status or "Unknown",
                "EstimatedDeparture": est_dep,
                "EstimatedArrival": est_arr,
            }

        # Case 2: flights dict
        if isinstance(data, dict) and "flights" in data:
            flights = data.get("flights") or []
            if not flights:
                return {"Status": "Not Found", "EstimatedDeparture": None, "EstimatedArrival": None}

            first = flights[0]
            status = first.get("displayStatus") or first.get("status") or "Unknown"

            est_dep = first.get("departureTime")
            est_arr = first.get("arrivalTime")

            return {
                "Status": status,
                "EstimatedDeparture": est_dep,
                "EstimatedArrival": est_arr,
            }

        return {"Status": "Not Found", "EstimatedDeparture": None, "EstimatedArrival": None}

    except Exception as e:
        return {"Status": f"Error: {str(e)}", "EstimatedDeparture": None, "EstimatedArrival": None}


# ==========================================
#  AMADEUS — FLIGHT ANALYSIS
# ==========================================

AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")

AMADEUS_AUTH_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
AMADEUS_FLIGHT_OFFERS_URL = "https://test.api.amadeus.com/v2/shopping/flight-offers"

amadeus_token = None
amadeus_token_expiry = 0


def get_amadeus_token():
    """Fetch or refresh OAuth token securely."""
    global amadeus_token, amadeus_token_expiry

    if not AMADEUS_CLIENT_ID or not AMADEUS_CLIENT_SECRET:
        raise RuntimeError("Amadeus API credentials missing")

    now = time.time()
    if amadeus_token and now < amadeus_token_expiry:
        return amadeus_token

    data = {
        "grant_type": "client_credentials",
        "client_id": AMADEUS_CLIENT_ID,
        "client_secret": AMADEUS_CLIENT_SECRET,
    }

    resp = requests.post(AMADEUS_AUTH_URL, data=data, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"Amadeus auth failed: {resp.text}")

    payload = resp.json()
    amadeus_token = payload.get("access_token")
    amadeus_token_expiry = now + payload.get("expires_in", 1800) - 60

    return amadeus_token


def search_lowest_fare_amadeus(origin, destination, depart_date, return_date):
    """Return lowest fare for (origin → destination)."""
    try:
        token = get_amadeus_token()
    except Exception as e:
        return {"Error": f"Auth error: {e}"}

    params = {
        "originLocationCode": origin,
        "destinationLocationCode": destination,
        "departureDate": depart_date,
        "returnDate": return_date,
        "adults": 1,
        "currencyCode": "USD",
        "max": 1,
    }

    resp = requests.get(
        AMADEUS_FLIGHT_OFFERS_URL,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=20,
    )

    if resp.status_code != 200:
        return {"Error": f"API error {resp.status_code}"}

    data = resp.json().get("data", [])
    if not data:
        return {"Error": "No fares found"}

    offer = data[0]
    price = offer.get("price", {}).get("grandTotal")
    currency = offer.get("price", {}).get("currency", "USD")

    return {
        "Price": price,
        "Currency": currency,
        "Error": "",
    }


# ==========================================
#  HOME PAGE
# ==========================================

@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        company = request.form.get("company", "").strip().lower()
        if not is_company_allowed(company):
            flash("Access denied.")
            return render_template("index.html")
        return redirect(url_for("flight_status", company=company))
    return render_template("index.html")


# ==========================================
#  FLIGHT STATUS PAGE
# ==========================================

@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    global last_results

    company = (request.args.get("company") or request.form.get("company") or "").lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    flight_info = None

    if request.method == "POST":
        airline = request.form.get("airline", "").upper()
        flight_number = request.form.get("flight_number", "").strip()
        departure = request.form.get("departure", "").upper()
        arrival = request.form.get("arrival", "").upper()

        result = fetch_status_flightapi(airline, flight_number)

        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": departure,
            "To": arrival,
            **result,
        }

        last_results = [flight_info]

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
    )


# ==========================================
#  EXCEL UPLOAD
# ==========================================

@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results

    company = (request.args.get("company") or request.form.get("company") or "").lower()
    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    file = request.files.get("file")
    if not file:
        flash("No file uploaded.")
        return redirect(url_for("flight_status", company=company))

    try:
        df = pd.read_excel(file) if file.filename.endswith(".xlsx") else pd.read_csv(file)
        df.columns = df.columns.str.lower()

        required = ["airline", "flightnumber", "departure", "arrival"]
        if any(col not in df.columns for col in required):
            flash("Missing required columns.")
            return redirect(url_for("flight_status", company=company))

        results = []
        for _, row in df.iterrows():
            airline = str(row["airline"]).upper()
            flight_number = str(row["flightnumber"])
            departure = str(row["departure"]).upper()
            arrival = str(row["arrival"]).upper()

            result = fetch_status_flightapi(airline, flight_number)

            results.append({
                "Airline": airline,
                "FlightNumber": flight_number,
                "From": departure,
                "To": arrival,
                **result,
            })

        last_results = results

        return render_template(
            "flight_status.html",
            company=company,
            uploaded_results=results,
        )

    except Exception as e:
        flash(f"Error processing file: {e}")
        return redirect(url_for("flight_status", company=company))


# ==========================================
#  DOWNLOAD STATUS
# ==========================================

@app.route("/download")
def download_excel():
    df = pd.DataFrame(last_results)
    out = BytesIO()
    df.to_excel(out, index=False)
    out.seek(0)
    return send_file(out, download_name="flight_status.xlsx", as_attachment=True)


# ==========================================
#  FLIGHT ANALYSIS PAGE
# ==========================================

@app.route("/flight-analysis", methods=["GET", "POST"])
def flight_analysis():
    global last_analysis_results

    company = (request.args.get("company") or request.form.get("company") or "").lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    analysis_results = None

    if request.method == "POST":
        origins = [o.upper() for o in request.form.getlist("origins") if o.strip()]
        dests = [d.upper() for d in request.form.getlist("destinations") if d.strip()]

        depart_date = request.form.get("outbound_date", "")
        return_date = request.form.get("return_date", "")

        if not origins or not dests:
            flash("Enter at least one origin and one destination.")
            return render_template("flight_analysis.html", company=company)

        analysis = []
        for o in origins:
            for d in dests:
                result = search_lowest_fare_amadeus(o, d, depart_date, return_date)
                analysis.append({
                    "Origin": o,
                    "Destination": d,
                    "Price": result.get("Price"),
                    "Currency": result.get("Currency"),
                    "Error": result.get("Error"),
                })

        last_analysis_results = analysis
        analysis_results = analysis

    return render_template(
        "flight_analysis.html",
        company=company,
        analysis_results=analysis_results,
    )


# ==========================================
#  DOWNLOAD ANALYSIS
# ==========================================

@app.route("/download-analysis")
def download_analysis_excel():
    df = pd.DataFrame(last_analysis_results)
    out = BytesIO()
    df.to_excel(out, index=False)
    out.seek(0)
    return send_file(out, download_name="flight_analysis.xlsx", as_attachment=True)


# ==========================================
#  RUN
# ==========================================

if __name__ == "__main__":
    app.run(debug=True)

