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

# ======================================================
# Allowed Companies
# ======================================================
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
last_results = []
last_analysis_results = []


def is_company_allowed(company: str) -> bool:
    if not ALLOWED_COMPANIES:
        return True
    if not company:
        return False
    return company.lower() in ALLOWED_COMPANIES


# ======================================================
# FlightAPI.io (Flight Status)
# ======================================================
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY", "69175603253bb1627f7ea9cc")


def normalize_status(status):
    """Normalize case + convert numeric statuses to readable text."""
    if isinstance(status, int):
        return {
            1: "Scheduled",
            2: "Arrived",
            3: "Departed",
            4: "Delayed",
            5: "Cancelled",
        }.get(status, "Unknown")

    if not status:
        return "Unknown"

    return str(status).strip().title()


def fetch_status_flightapi(airline: str, flight_number: str, flight_date: str):
    """
    Returns dict:
    {
        "Status": "Delayed",
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
            return {"Status": "API Error"}

        data = resp.json()

        # Case 1 — list response
        if isinstance(data, list):
            status_val = None
            est_dep = None
            est_arr = None

            for block in data:
                if "departure" in block:
                    est_dep = block["departure"].get("estimatedTime")
                if "arrival" in block:
                    est_arr = block["arrival"].get("estimatedTime")
                if "status" in block:
                    status_val = block["status"]

            return {
                "Status": normalize_status(status_val),
                "EstimatedDeparture": est_dep,
                "EstimatedArrival": est_arr,
            }

        # Case 2 — dict with flights array
        if isinstance(data, dict) and "flights" in data:
            flights = data.get("flights", [])
            if not flights:
                return {"Status": "Not Found"}

            f = flights[0]

            status_val = normalize_status(f.get("displayStatus") or f.get("status"))
            est_dep = f.get("departureTime")
            est_arr = f.get("arrivalTime")

            return {
                "Status": status_val,
                "EstimatedDeparture": est_dep,
                "EstimatedArrival": est_arr,
            }

        return {"Status": "Not Found"}

    except Exception:
        return {"Status": "Error"}


# ======================================================
# Home Page
# ======================================================
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


# ======================================================
# Flight Status Page
# ======================================================
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
        airline = request.form.get("airline", "").upper().strip()
        flight_number = request.form.get("flight_number", "").strip()
        departure = request.form.get("departure", "").upper().strip()
        arrival = request.form.get("arrival", "").upper().strip()

        if not all([airline, flight_number, departure, arrival]):
            flash("All fields are required.")
            return render_template("flight_status.html", company=company)

        today = datetime.today().strftime("%Y-%m-%d")

        status_data = fetch_status_flightapi(airline, flight_number, today)

        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": departure,
            "To": arrival,
            "Status": status_data.get("Status"),
            "EstimatedDeparture": status_data.get("EstimatedDeparture"),
            "EstimatedArrival": status_data.get("EstimatedArrival"),
        }

        last_results = [flight_info]

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results,
    )


# ======================================================
# Excel Upload
# ======================================================
@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results

    company = (request.args.get("company") or request.form.get("company") or "").strip().lower()
    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    file = request.files.get("file")
    if not file or not file.filename:
        flash("No file uploaded.")
        return redirect(url_for("flight_status", company=company))

    try:
        df = pd.read_excel(file) if file.filename.lower().endswith(".xlsx") else pd.read_csv(file)
        df.columns = df.columns.str.strip().str.lower()

        required = ["airline", "flightnumber", "departure", "arrival"]
        if any(col not in df.columns for col in required):
            flash("Missing required columns in Excel.")
            return redirect(url_for("flight_status", company=company))

        today = datetime.today().strftime("%Y-%m-%d")
        results = []

        for _, row in df.iterrows():
            status_data = fetch_status_flightapi(
                str(row["airline"]).upper(),
                str(row["flightnumber"]).strip(),
                today,
            )

            results.append({
                "Airline": row["airline"],
                "FlightNumber": row["flightnumber"],
                "From": row["departure"],
                "To": row["arrival"],
                "Status": status_data.get("Status"),
                "EstimatedDeparture": status_data.get("EstimatedDeparture"),
                "EstimatedArrival": status_data.get("EstimatedArrival"),
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


# ======================================================
# Download Excel
# ======================================================
@app.route("/download")
def download_excel():
    if not last_results:
        flash("No results to download.")
        return redirect(url_for("home"))

    df = pd.DataFrame(last_results)
    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(output, download_name="flight_status_results.xlsx", as_attachment=True)


# ======================================================
# Run App
# ======================================================
if __name__ == "__main__":
    app.run(debug=True)
