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

# ============================================
# ALLOWED COMPANIES
# ============================================
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

# Storage for exporting
last_results = []
last_analysis_results = []


def is_company_allowed(company: str) -> bool:
    if not ALLOWED_COMPANIES:
        return True
    if not company:
        return False
    return company.lower() in ALLOWED_COMPANIES


# ============================================
# FlightAPI.io — STATUS API
# ============================================
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY", "69175603253bb1627f7ea9cc")


def fetch_status_flightapi(airline, flight_number, flight_date):
    """
    Returns a CLEAN object:

    {
        "Status": "Delayed",
        "EstimatedDeparture": "14:10, Nov 26",
        "EstimatedArrival": "16:45, Nov 26"
    }

    Never returns dict into Status column.
    """

    airline_code = airline.upper()
    date_str = flight_date.replace("-", "")

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline_code}&date={date_str}"
    )

    try:
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            return {
                "Status": f"API Error ({r.status_code})",
                "EstimatedDeparture": "",
                "EstimatedArrival": "",
            }

        data = r.json()

        # ---------- CASE 1: List style ----------
        if isinstance(data, list):
            status = None
            est_dep = None
            est_arr = None

            for block in data:
                if "status" in block:
                    status = block["status"]
                if "departure" in block:
                    est_dep = block["departure"].get("estimatedTime") or block["departure"].get("scheduledTime")
                if "arrival" in block:
                    est_arr = block["arrival"].get("estimatedTime") or block["arrival"].get("scheduledTime")

            return {
                "Status": status or "Unknown",
                "EstimatedDeparture": est_dep or "",
                "EstimatedArrival": est_arr or "",
            }

        # ---------- CASE 2: flights[] ----------
        if isinstance(data, dict) and "flights" in data:
            flights = data.get("flights") or []
            if not flights:
                return {"Status": "Not Found", "EstimatedDeparture": "", "EstimatedArrival": ""}

            f = flights[0]

            status = f.get("displayStatus") or f.get("status")

            est_dep = f.get("departureTime") or ""
            est_arr = f.get("arrivalTime") or ""

            return {
                "Status": status or "Unknown",
                "EstimatedDeparture": est_dep,
                "EstimatedArrival": est_arr,
            }

        return {"Status": "Not Found", "EstimatedDeparture": "", "EstimatedArrival": ""}

    except Exception as e:
        return {
            "Status": f"Error: {e}",
            "EstimatedDeparture": "",
            "EstimatedArrival": "",
        }


# ============================================
# Amadeus — FLIGHT ANALYSIS
# ============================================
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID", "")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET", "")

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
        timeout=15,
    )

    if resp.status_code != 200:
        raise Exception(f"Amadeus auth failed: {resp.text}")

    token_data = resp.json()
    amadeus_token = token_data.get("access_token")
    amadeus_token_expiry = now + token_data.get("expires_in", 1800) - 60
    return amadeus_token


def search_lowest_fare_amadeus(origin, dest, date_out, date_ret):
    try:
        token = get_amadeus_token()
    except Exception as e:
        return {"Error": f"Auth error: {e}"}

    resp = requests.get(
        AMADEUS_FLIGHT_OFFERS_URL,
        headers={"Authorization": f"Bearer {token}"},
        params={
            "originLocationCode": origin,
            "destinationLocationCode": dest,
            "departureDate": date_out,
            "returnDate": date_ret,
            "adults": 1,
            "currencyCode": "USD",
            "max": 1,
        },
        timeout=25,
    )

    if resp.status_code != 200:
        return {"Error": f"API error {resp.status_code}"}

    data = resp.json()
    offers = data.get("data", [])

    if not offers:
        return {"Error": "No fares found"}

    price = offers[0].get("price", {}).get("grandTotal")
    currency = offers[0].get("price", {}).get("currency", "USD")

    return {"Price": price, "Currency": currency, "Error": ""}


# ============================================
# HOME PAGE
# ============================================
@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        company = request.form.get("company", "").strip().lower()

        if not company:
            flash("Enter a company name.")
            return render_template("index.html")

        if not is_company_allowed(company):
            flash("Access denied.")
            return render_template("index.html")

        return redirect(url_for("flight_status", company=company))

    return render_template("index.html")


# ============================================
# FLIGHT STATUS PAGE
# ============================================
@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    global last_results

    company = (request.args.get("company") or "").lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    flight_info = None
    uploaded_results = None

    if request.method == "POST":
        airline = request.form.get("airline", "").strip().upper()
        flight_number = request.form.get("flight_number", "").strip()
        dep = request.form.get("departure", "").strip().upper()
        arr = request.form.get("arrival", "").strip().upper()

        today = datetime.today().strftime("%Y-%m-%d")

        raw = fetch_status_flightapi(airline, flight_number, today)

        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": dep,
            "To": arr,
            "Status": raw["Status"],
            "EstimatedDeparture": raw["EstimatedDeparture"],
            "EstimatedArrival": raw["EstimatedArrival"],
        }

        last_results = [flight_info]

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results,
    )


# ============================================
# UPLOAD EXCEL FOR STATUS
# ============================================
@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results

    company = (request.args.get("company") or "").lower()
    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    if "file" not in request.files:
        flash("No file uploaded.")
        return redirect(url_for("flight_status", company=company))

    file = request.files["file"]

    df = pd.read_excel(file) if file.filename.endswith(".xlsx") else pd.read_csv(file)
    df.columns = df.columns.str.lower().str.strip()

    required = ["airline", "flightnumber", "departure", "arrival"]
    if any(col not in df.columns for col in required):
        flash("Missing required columns.")
        return redirect(url_for("flight_status", company=company))

    today = datetime.today().strftime("%Y-%m-%d")

    results = []
    for _, row in df.iterrows():
        airline = row["airline"].strip().upper()
        num = str(row["flightnumber"])
        dep = row["departure"].strip().upper()
        arr = row["arrival"].strip().upper()

        raw = fetch_status_flightapi(airline, num, today)

        results.append({
            "Airline": airline,
            "FlightNumber": num,
            "From": dep,
            "To": arr,
            "Status": raw["Status"],
            "EstimatedDeparture": raw["EstimatedDeparture"],
            "EstimatedArrival": raw["EstimatedArrival"],
        })

    last_results = results

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=None,
        uploaded_results=results,
    )


# ============================================
# DOWNLOAD STATUS EXCEL
# ============================================
@app.route("/download")
def download_excel():
    df = pd.DataFrame(last_results)
    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(output, download_name="flight_status.xlsx", as_attachment=True)


# ============================================
# FLIGHT ANALYSIS PAGE
# ============================================
@app.route("/flight-analysis", methods=["GET", "POST"])
def flight_analysis():
    global last_analysis_results

    company = (request.args.get("company") or "").lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    results = None

    if request.method == "POST":
        origins = [o.strip().upper() for o in request.form.getlist("origins") if o.strip()]
        dests = [d.strip().upper() for d in request.form.getlist("destinations") if d.strip()]
        d_out = request.form.get("outbound_date")
        d_ret = request.form.get("return_date")

        if not origins or not dests:
            flash("Enter at least one origin and one destination.")
            return render_template("flight_analysis.html", company=company)

        results = []
        for o in origins:
            for d in dests:
                r = search_lowest_fare_amadeus(o, d, d_out, d_ret)

                results.append({
                    "Origin": o,
                    "Destination": d,
                    "Price": r.get("Price"),
                    "Currency": r.get("Currency", "USD"),
                    "Error": r.get("Error", ""),
                })

        last_analysis_results = results

    return render_template(
        "flight_analysis.html",
        company=company,
        analysis_results=results,
    )


# ============================================
# DOWNLOAD ANALYSIS
# ============================================
@app.route("/download-analysis-excel")
def download_analysis_excel():
    df = pd.DataFrame(last_analysis_results)
    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(output, download_name="flight_analysis.xlsx", as_attachment=True)


# ============================================
# RUN
# ============================================
if __name__ == "__main__":
    app.run(debug=True)

