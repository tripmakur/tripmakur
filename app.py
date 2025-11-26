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
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this-secret")

# ====================================================
# Allowed Companies
# ====================================================
ALLOWED_COMPANIES_FILE = "allowed_companies.json"


def load_allowed_companies():
    """Load allowed companies from JSON file."""
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        try:
            with open(ALLOWED_COMPANIES_FILE, "r") as f:
                data = json.load(f)
                return [str(c).strip().lower() for c in data]
        except Exception:
            return []
    return []


ALLOWED_COMPANIES = load_allowed_companies()

# results storage for excel downloads
last_results = []
last_analysis_results = []


def is_company_allowed(company: str) -> bool:
    """Check if user company is authorized."""
    if not ALLOWED_COMPANIES:
        return True
    if not company:
        return False
    return company.lower() in ALLOWED_COMPANIES


# ====================================================
# FlightAPI.io Settings
# ====================================================
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY")  # stored in Render environment variables


def fetch_status_flightapi(airline: str, flight_number: str, flight_date: str):
    """Fetch flight status + estimated times from FlightAPI.io."""
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

        # Case 1 — list style response (common)
        if isinstance(data, list):
            status = "Unknown"
            est_dep = None
            est_arr = None

            for block in data:
                if "status" in block:
                    status = block["status"]

                if "departure" in block and isinstance(block["departure"], dict):
                    est_dep = block["departure"].get("estimatedTime")

                if "arrival" in block and isinstance(block["arrival"], dict):
                    est_arr = block["arrival"].get("estimatedTime")

            return {
                "Status": status,
                "EstimatedDeparture": est_dep,
                "EstimatedArrival": est_arr,
            }

        # Case 2 — dict with flights[]
        if isinstance(data, dict) and "flights" in data:
            flights = data.get("flights") or []
            if not flights:
                return {
                    "Status": "Not Found",
                    "EstimatedDeparture": None,
                    "EstimatedArrival": None,
                }

            first = flights[0]
            status = first.get("displayStatus") or first.get("status") or "Unknown"

            dep = first.get("departureTime")
            arr = first.get("arrivalTime")

            return {
                "Status": status,
                "EstimatedDeparture": dep,
                "EstimatedArrival": arr,
            }

        return {
            "Status": "Not Found",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }

    except Exception as e:
        return {
            "Status": f"Error: {e}",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }


# ====================================================
# Amadeus API (Flight Analysis)
# ====================================================
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")

AMADEUS_AUTH_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
AMADEUS_OFFER_URL = "https://test.api.amadeus.com/v2/shopping/flight-offers"

amadeus_token = None
amadeus_expiry = 0


def get_amadeus_token():
    """Obtain Amadeus OAuth token."""
    global amadeus_token, amadeus_expiry

    if not AMADEUS_CLIENT_ID or not AMADEUS_CLIENT_SECRET:
        raise RuntimeError("Amadeus credentials missing in environment variables.")

    now = time.time()
    if amadeus_token and now < amadeus_expiry:
        return amadeus_token

    payload = {
        "grant_type": "client_credentials",
        "client_id": AMADEUS_CLIENT_ID,
        "client_secret": AMADEUS_CLIENT_SECRET,
    }

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    resp = requests.post(AMADEUS_AUTH_URL, data=payload, headers=headers, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"Amadeus auth failed: {resp.text}")

    json_data = resp.json()
    amadeus_token = json_data["access_token"]
    amadeus_expiry = now + json_data.get("expires_in", 1800) - 60

    return amadeus_token


def search_lowest_fare_amadeus(origin, dest, dep_date, ret_date):
    """Search the lowest fare between two airports."""
    try:
        token = get_amadeus_token()
    except Exception as e:
        return {"Error": f"Auth error: {e}"}

    params = {
        "originLocationCode": origin,
        "destinationLocationCode": dest,
        "departureDate": dep_date,
        "returnDate": ret_date,
        "adults": 1,
        "max": 1,
        "currencyCode": "USD",
    }

    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = requests.get(AMADEUS_OFFER_URL, params=params, headers=headers, timeout=20)

        if resp.status_code != 200:
            return {"Error": f"API error {resp.status_code}"}

        json_data = resp.json()
        offers = json_data.get("data", [])

        if not offers:
            return {"Error": "No fares found"}

        offer = offers[0]
        price = offer.get("price", {}).get("grandTotal")
        currency = offer.get("price", {}).get("currency", "USD")

        return {"Price": price, "Currency": currency}

    except Exception as e:
        return {"Error": str(e)}


# ====================================================
# Routes
# ====================================================

@app.route("/", methods=["GET", "POST"])
def home():
    """Company login page."""
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


# ====================================================
# Flight Status Page
# ====================================================
@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    """Flight status lookup + form."""
    global last_results

    company = request.args.get("company", "").strip().lower()

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

        if not all([airline, flight_number, dep, arr]):
            flash("All fields are required.")
            return render_template("flight_status.html", company=company)

        today = datetime.now().astimezone().strftime("%Y-%m-%d")
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


# ====================================================
# Excel Upload
# ====================================================
@app.route("/upload", methods=["POST"])
def upload_file():
    """Upload Excel/CSV and check status."""
    global last_results

    company = request.args.get("company", "").strip().lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    if "file" not in request.files:
        flash("No file uploaded.")
        return redirect(url_for("flight_status", company=company))

    file = request.files["file"]

    try:
        if file.filename.lower().endswith(".csv"):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file)

        df.columns = df.columns.str.lower().str.strip()

        required = ["airline", "flightnumber", "departure", "arrival"]
        for col in required:
            if col not in df.columns:
                flash(f"Missing column: {col}")
                return redirect(url_for("flight_status", company=company))

        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        results = []

        for _, row in df.iterrows():
            airline = str(row["airline"]).strip().upper()
            fn = str(row["flightnumber"]).strip()
            dep = str(row["departure"]).strip().upper()
            arr = str(row["arrival"]).strip().upper()

            status_data = fetch_status_flightapi(airline, fn, today)

            results.append(
                {
                    "Airline": airline,
                    "FlightNumber": fn,
                    "From": dep,
                    "To": arr,
                    "Status": status_data["Status"],
                    "EstimatedDeparture": status_data["EstimatedDeparture"],
                    "EstimatedArrival": status_data["EstimatedArrival"],
                }
            )

        last_results = results

        return render_template(
            "flight_status.html",
            company=company,
            flight_info=None,
            uploaded_results=results,
        )

    except Exception as e:
        flash(f"Error: {e}")
        return redirect(url_for("flight_status", company=company))


# ====================================================
# Download Excel
# ====================================================
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
        as_attachment=True,
        download_name="flight_status_results.xlsx",
    )


# ====================================================
# Flight Analysis Page
# ====================================================
@app.route("/flight-analysis", methods=["GET", "POST"])
def flight_analysis():
    """Flight Analysis UI + Amadeus search."""
    global last_analysis_results

    company = request.args.get("company", "").strip().lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    results = None

    if request.method == "POST":
        origins = [
            o.strip().upper()
            for o in request.form.getlist("origins")
            if o.strip()
        ]
        dests = [
            d.strip().upper()
            for d in request.form.getlist("destinations")
            if d.strip()
        ]
        dep_date = request.form.get("outbound_date", "").strip()
        ret_date = request.form.get("return_date", "").strip()

        if not origins or not dests:
            flash("Enter at least one origin and one destination.")
            return render_template("flight_analysis.html", company=company)

        if not dep_date or not ret_date:
            flash("Enter both outbound and return dates.")
            return render_template("flight_analysis.html", company=company)

        list_results = []
        for o in origins:
            for d in dests:
                r = search_lowest_fare_amadeus(o, d, dep_date, ret_date)
                list_results.append(
                    {
                        "Origin": o,
                        "Destination": d,
                        "Price": r.get("Price"),
                        "Currency": r.get("Currency"),
                        "Error": r.get("Error"),
                    }
                )

        last_analysis_results = list_results
        results = list_results

    return render_template(
        "flight_analysis.html",
        company=company,
        analysis_results=results,
    )


# ====================================================
# Download Flight Analysis Excel
# ====================================================
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
        as_attachment=True,
        download_name="flight_analysis_results.xlsx",
    )


# ====================================================
# Run Locally
# ====================================================
if __name__ == "__main__":
    app.run(debug=True)


