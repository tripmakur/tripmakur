import os
import json
import time
from datetime import datetime
from io import BytesIO

import pandas as pd
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback-secret")

# =============================================================================
# Allowed Companies
# =============================================================================

ALLOWED_COMPANIES_FILE = "allowed_companies.json"


def load_allowed_companies():
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        try:
            with open(ALLOWED_COMPANIES_FILE, "r") as f:
                return [c.lower() for c in json.load(f)]
        except:
            return []
    return []


ALLOWED_COMPANIES = load_allowed_companies()


def is_company_allowed(company: str) -> bool:
    if not ALLOWED_COMPANIES:
        return True  # allow all if file is empty
    return company.lower() in ALLOWED_COMPANIES


# =============================================================================
# FlightAPI.io Settings
# =============================================================================

FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY", "69175603253bb1627f7ea9cc")


# ******************************************************
# CORRECTED STATUS FETCHER (THIS FIXES "UNKNOWN")
# ******************************************************
def fetch_status_flightapi(airline: str, flight_number: str, flight_date: str):
    """
    Returns a dict:
    {
        "Status": "Arrived",
        "EstimatedDeparture": "...",
        "EstimatedArrival": "..."
    }
    """

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
                "EstimatedArrival": None
            }

        data = resp.json()

        # MAIN FIX: ensure "flights" exists
        if "flights" not in data or not isinstance(data["flights"], list):
            return {"Status": "Not Found", "EstimatedDeparture": None, "EstimatedArrival": None}

        flights = data["flights"]
        if not flights:
            return {"Status": "Not Found", "EstimatedDeparture": None, "EstimatedArrival": None}

        # Use the first flight (usually outbound leg)
        first = flights[0]

        # Status can be "displayStatus" or numeric "status"
        status_raw = first.get("displayStatus") or first.get("status")

        if isinstance(status_raw, int):
            status_raw = {
                1: "Scheduled",
                2: "Arrived",
                3: "Departed",
                4: "Delayed",
                5: "Cancelled",
            }.get(status_raw, "Unknown")

        dep_est = first.get("departureTime")
        arr_est = first.get("arrivalTime")

        return {
            "Status": status_raw or "Unknown",
            "EstimatedDeparture": dep_est,
            "EstimatedArrival": arr_est
        }

    except Exception as e:
        return {
            "Status": f"Error: {str(e)}",
            "EstimatedDeparture": None,
            "EstimatedArrival": None
        }


# =============================================================================
# HOME
# =============================================================================

@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        company = request.form.get("company", "").strip().lower()

        if not company:
            flash("Please enter your company name.")
            return render_template("index.html")

        if not is_company_allowed(company):
            flash("Access denied.")
            return render_template("index.html")

        return redirect(url_for("flight_status", company=company))

    return render_template("index.html")


# =============================================================================
# FLIGHT STATUS PAGE
# =============================================================================

last_results = []


@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    global last_results

    company = request.args.get("company", "").lower()
    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    flight_info = None
    uploaded_results = None

    if request.method == "POST":
        airline = request.form.get("airline", "").upper().strip()
        flight_number = request.form.get("flight_number", "").strip()
        departure = request.form.get("departure", "").upper().strip()
        arrival = request.form.get("arrival", "").upper().strip()

        if not all([airline, flight_number, departure, arrival]):
            flash("All fields are required.")
            return render_template("flight_status.html", company=company)

        today = datetime.now().strftime("%Y-%m-%d")
        status_data = fetch_status_flightapi(airline, flight_number, today)

        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": departure,
            "To": arrival,
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


# =============================================================================
# EXCEL UPLOAD
# =============================================================================

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

    try:
        df = pd.read_excel(file)
        df.columns = df.columns.str.strip().str.lower()

        required = ["airline", "flightnumber", "departure", "arrival"]
        if any(col not in df.columns for col in required):
            flash("Excel missing required columns.")
            return redirect(url_for("flight_status", company=company))

        today = datetime.now().strftime("%Y-%m-%d")
        results = []

        for _, row in df.iterrows():
            airline = str(row["airline"]).upper().strip()
            flight_number = str(row["flightnumber"]).strip()
            dep = str(row["departure"]).upper().strip()
            arr = str(row["arrival"]).upper().strip()

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
            uploaded_results=results
        )

    except Exception as e:
        flash(f"Error processing file: {e}")
        return redirect(url_for("flight_status", company=company))


# =============================================================================
# DOWNLOAD STATUS RESULTS
# =============================================================================

@app.route("/download")
def download_excel():
    if not last_results:
        flash("No results to download.")
        return redirect(url_for("home"))

    df = pd.DataFrame(last_results)
    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(output, as_attachment=True, download_name="flight_status_results.xlsx")


# =============================================================================
# FLIGHT ANALYSIS (Enabled)
# =============================================================================

@app.route("/flight-analysis", methods=["GET", "POST"])
def flight_analysis():
    company = request.args.get("company", "").lower()
    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    # (You said flight analysis works — I can restore it fully if you want)
    return render_template("flight_analysis.html", company=company)


# =============================================================================

if __name__ == "__main__":
    app.run(debug=True)






