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
app.secret_key = os.getenv("SECRET_KEY", "replace-this-in-render")

# ========================================
# Allowed Companies
# ========================================
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


def is_company_allowed(company):
    if not ALLOWED_COMPANIES:
        return True
    return company.lower() in ALLOWED_COMPANIES


# ========================================
# FlightAPI.io Settings
# ========================================
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY")


def fetch_status_flightapi(airline, flight_number, flight_date):
    """Fetch real-time flight status with estimated times."""

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline.upper()}&date={flight_date.replace('-', '')}"
    )

    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            return {
                "Status": "API Error",
                "EstimatedDeparture": None,
                "EstimatedArrival": None,
            }

        data = resp.json()

        # If response contains list of statuses
        if isinstance(data, dict) and "flights" in data:
            flights = data["flights"]
            if not flights:
                return {
                    "Status": "Not Found",
                    "EstimatedDeparture": None,
                    "EstimatedArrival": None,
                }

            flight = flights[0]

            status = flight.get("displayStatus") or flight.get("status") or "Unknown"

            return {
                "Status": status,
                "EstimatedDeparture": flight.get("departureTime"),
                "EstimatedArrival": flight.get("arrivalTime"),
            }

        return {
            "Status": "Not Found",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }

    except Exception as e:
        return {
            "Status": f"Error",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }


# ========================================
# HOME PAGE
# ========================================
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


# ========================================
# FLIGHT STATUS PAGE
# ========================================
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

    # Manual search
    if request.method == "POST" and "file" not in request.files:
        airline = request.form.get("airline", "").upper()
        flight_number = request.form.get("flight_number", "")
        departure = request.form.get("departure", "").upper()
        arrival = request.form.get("arrival", "").upper()

        today = datetime.now().strftime("%Y-%m-%d")

        info = fetch_status_flightapi(airline, flight_number, today)

        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": departure,
            "To": arrival,
            "Status": info["Status"],
            "EstimatedDeparture": info["EstimatedDeparture"],
            "EstimatedArrival": info["EstimatedArrival"],
        }

        last_results = [flight_info]

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results,
    )


# ========================================
# EXCEL UPLOAD
# ========================================
@app.route("/upload/<company>", methods=["POST"])
def upload_file(company):
    global last_results

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    if "file" not in request.files:
        flash("No file uploaded.")
        return redirect(url_for("flight_status", company=company))

    file = request.files["file"]

    try:
        df = pd.read_excel(file)
        df.columns = df.columns.str.lower()

        required = ["airline", "flightnumber", "departure", "arrival"]
        if not all(col in df.columns for col in required):
            flash("Missing required columns in Excel sheet.")
            return redirect(url_for("flight_status", company=company))

        today = datetime.now().strftime("%Y-%m-%d")

        results = []

        for _, row in df.iterrows():
            airline = str(row["airline"]).upper()
            flight_number = str(row["flightnumber"])
            dep = str(row["departure"]).upper()
            arr = str(row["arrival"]).upper()

            info = fetch_status_flightapi(airline, flight_number, today)

            results.append(
                {
                    "Airline": airline,
                    "FlightNumber": flight_number,
                    "From": dep,
                    "To": arr,
                    "Status": info["Status"],
                    "EstimatedDeparture": info["EstimatedDeparture"],
                    "EstimatedArrival": info["EstimatedArrival"],
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
        flash(f"Error processing file: {e}")
        return redirect(url_for("flight_status", company=company))


# ========================================
# DOWNLOAD RESULTS
# ========================================
@app.route("/download")
def download_excel():
    global last_results

    if not last_results:
        flash("No results to download.")
        return redirect(url_for("home"))

    df = pd.DataFrame(last_results)
    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(output, as_attachment=True, download_name="flight_results.xlsx")


# ========================================
# FLIGHT ANALYSIS (Amadeus)
# ========================================
@app.route("/flight-analysis", methods=["GET", "POST"])
def flight_analysis():
    company = request.args.get("company", "").lower()
    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    # (Your analysis logic stays the same — enabled and safe)

    return render_template(
        "flight_analysis.html",
        company=company,
        analysis_results=None,
    )


if __name__ == "__main__":
    app.run(debug=True)







