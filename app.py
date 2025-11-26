import os
import json
from datetime import datetime
from io import BytesIO

import pandas as pd
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")

# -------------------------------------------------------------------
# LOAD ALLOWED COMPANIES
# -------------------------------------------------------------------
def load_allowed_companies():
    file_path = os.path.join("templates", "allowed_companies.json")
    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as f:
                data = json.load(f)
                return [x.lower().strip() for x in data]
        except:
            return []
    return []

ALLOWED_COMPANIES = load_allowed_companies()


def is_company_allowed(company):
    if not ALLOWED_COMPANIES:
        return True
    return company.lower() in ALLOWED_COMPANIES


# -------------------------------------------------------------------
# FLIGHTAPI SETTINGS
# -------------------------------------------------------------------
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY")

STATUS_MAP = {
    1: "Scheduled",
    2: "Arrived",
    3: "Departed",
    4: "Delayed",
    5: "Cancelled"
}


def fetch_flight_status(airline, flight_number):
    """Fetches status + updated times if delayed."""

    today = datetime.now().strftime("%Y%m%d")
    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline}&date={today}"
    )

    try:
        resp = requests.get(url, timeout=20)
        data = resp.json()

        # Case 1 — JSON list
        if isinstance(data, list):
            status_block = next((x for x in data if "status" in x), None)
            if not status_block:
                return {"Status": "Unknown"}

            status = status_block.get("status") or "Unknown"
            dep_time = status_block.get("estimatedDeparture")
            arr_time = status_block.get("estimatedArrival")

            return {
                "Status": status,
                "EstimatedDeparture": dep_time,
                "EstimatedArrival": arr_time
            }

        # Case 2 — dict with flights
        if "flights" in data and data["flights"]:
            f = data["flights"][0]
            raw_status = f.get("displayStatus") or f.get("status")

            if isinstance(raw_status, int):
                status = STATUS_MAP.get(raw_status, "Unknown")
            else:
                status = raw_status or "Unknown"

            dep = f.get("estimatedDeparture") or f.get("departureTime")
            arr = f.get("estimatedArrival") or f.get("arrivalTime")

            return {
                "Status": status,
                "EstimatedDeparture": dep,
                "EstimatedArrival": arr
            }

        return {"Status": "Unknown"}

    except Exception as e:
        return {"Status": f"Error: {e}"}


# -------------------------------------------------------------------
# HOME
# -------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        company = request.form.get("company", "").lower().strip()

        if not company:
            flash("Enter company name")
            return render_template("index.html")

        if not is_company_allowed(company):
            flash("Access denied")
            return render_template("index.html")

        return redirect(url_for("flight_status", company=company))

    return render_template("index.html")


# -------------------------------------------------------------------
# FLIGHT STATUS PAGE
# -------------------------------------------------------------------
@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    company = request.args.get("company", "").lower().strip()
    if not is_company_allowed(company):
        flash("Access denied")
        return redirect(url_for("home"))

    flight_info = None
    uploaded_results = None

    # Manual lookup
    if request.method == "POST" and "airline" in request.form:
        airline = request.form["airline"].strip().upper()
        flight_no = request.form["flight_number"].strip()
        dep = request.form["departure"].strip().upper()
        arr = request.form["arrival"].strip().upper()

        status_data = fetch_flight_status(airline, flight_no)

        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_no,
            "From": dep,
            "To": arr,
            "Status": status_data["Status"],
            "EstimatedDeparture": status_data.get("EstimatedDeparture"),
            "EstimatedArrival": status_data.get("EstimatedArrival")
        }

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results
    )


# -------------------------------------------------------------------
# UPLOAD FILE ENDPOINT
# -------------------------------------------------------------------
@app.route("/upload", methods=["POST"])
def upload_file():
    company = request.args.get("company", "").lower().strip()
    if not is_company_allowed(company):
        flash("Access denied")
        return redirect(url_for("home"))

    if "file" not in request.files:
        flash("Upload a file")
        return redirect(url_for("flight_status", company=company))

    file = request.files["file"]

    # Read sheet into DataFrame
    try:
        if file.filename.lower().endswith(".csv"):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file)

        df.columns = df.columns.str.strip().str.lower()

        required = ["airline", "flightnumber", "departure", "arrival"]
        if not all(col in df.columns for col in required):
            flash("Missing required columns")
            return redirect(url_for("flight_status", company=company))

        results = []
        for _, row in df.iterrows():
            airline = str(row["airline"]).upper().strip()
            fl_no = str(row["flightnumber"]).strip()
            dep = str(row["departure"]).upper().strip()
            arr = str(row["arrival"]).upper().strip()

            status_data = fetch_flight_status(airline, fl_no)

            results.append({
                "Airline": airline,
                "FlightNumber": fl_no,
                "From": dep,
                "To": arr,
                "Status": status_data["Status"],
                "EstimatedDeparture": status_data.get("EstimatedDeparture"),
                "EstimatedArrival": status_data.get("EstimatedArrival"),
            })

        return render_template(
            "flight_status.html",
            company=company,
            uploaded_results=results,
            flight_info=None
        )

    except Exception as e:
        flash(f"Error reading file: {e}")
        return redirect(url_for("flight_status", company=company))


# -------------------------------------------------------------------
# DOWNLOAD RESULTS
# -------------------------------------------------------------------
@app.route("/download", methods=["POST"])
def download_excel():
    return "Not implemented yet"


if __name__ == "__main__":
    app.run(debug=True)



