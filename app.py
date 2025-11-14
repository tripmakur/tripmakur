import os
import json
import requests
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, send_file
)
import pandas as pd
from datetime import datetime
from io import BytesIO

app = Flask(__name__)
app.secret_key = "your-secret-key"

# ============================================================
# Allowed Companies
# ============================================================

ALLOWED_COMPANIES_FILE = "allowed_companies.json"


def load_allowed_companies():
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        with open(ALLOWED_COMPANIES_FILE, "r") as f:
            companies = json.load(f)
            return [c.strip().lower() for c in companies]
    return []


ALLOWED_COMPANIES = load_allowed_companies()
last_results = []

# ============================================================
# FlightAPI.io (Flight Tracking API via /airline)
# ============================================================

FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY")  # set in Render


def _extract_status_info(data):
    """
    Try to pull status + departure/arrival details from FlightAPI.io response.
    We handle both dict and list shapes defensively.
    Returns: (status, dep_info, arr_info)
    dep_info/arr_info are dicts with keys: airport, scheduled, estimated, terminal_gate
    """

    status = None
    dep_info = {"airport": None, "scheduled": None, "estimated": None, "terminal_gate": None}
    arr_info = {"airport": None, "scheduled": None, "estimated": None, "terminal_gate": None}

    # If it's a list, use the first element
    if isinstance(data, list):
        if not data:
            return status, dep_info, arr_info
        data = data[0]

    if not isinstance(data, dict):
        return status, dep_info, arr_info

    # Top-level status (if present)
    if isinstance(data.get("status"), str):
        status = data["status"]

    # Try "departure" field
    dep_list = data.get("departure") or data.get("departures")
    if isinstance(dep_list, list) and dep_list:
        d0 = dep_list[0]
        if isinstance(d0, dict):
            # Sometimes status is here
            if not status and isinstance(d0.get("status"), str):
                status = d0["status"]
            dep_info["airport"] = d0.get("Airport") or d0.get("airport")
            dep_info["scheduled"] = (
                d0.get("Scheduled Time")
                or d0.get("scheduledTime")
                or d0.get("scheduled")
            )
            dep_info["estimated"] = (
                d0.get("Estimated Time")
                or d0.get("estimatedTime")
                or d0.get("estimated")
            )
            dep_info["terminal_gate"] = (
                d0.get("Terminal - Gate")
                or d0.get("terminalGate")
                or d0.get("terminal")
                or d0.get("gate")
            )

    # Try "arrival" field
    arr_list = data.get("arrival") or data.get("arrivals")
    if isinstance(arr_list, list) and arr_list:
        a0 = arr_list[0]
        if isinstance(a0, dict):
            if not status and isinstance(a0.get("status"), str):
                status = a0["status"]
            arr_info["airport"] = a0.get("Airport") or a0.get("airport")
            arr_info["scheduled"] = (
                a0.get("Scheduled Time")
                or a0.get("scheduledTime")
                or a0.get("scheduled")
            )
            arr_info["estimated"] = (
                a0.get("Estimated Time")
                or a0.get("estimatedTime")
                or a0.get("estimated")
            )
            arr_info["terminal_gate"] = (
                a0.get("Terminal - Gate")
                or a0.get("terminalGate")
                or a0.get("terminal")
                or a0.get("gate")
            )

    return status, dep_info, arr_info


def fetch_status(airline, flight_number, flight_date):
    """
    Fetch flight status from FlightAPI.io using the /airline endpoint.

    FlightAPI.io example (from their docs):
    curl -X GET "https://api.flightapi.io/airline/API_KEY?num=4906&name=aa&date=20251114"
    """

    if not FLIGHTAPI_KEY:
        return {
            "status": "API Key Missing",
            "departure": {},
            "arrival": {},
        }

    # Convert YYYY-MM-DD -> YYYYMMDD
    date_compact = flight_date.replace("-", "")

    url = f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
    params = {
        "num": flight_number,
        "name": airline.lower(),  # AA -> aa
        "date": date_compact,
    }

    try:
        response = requests.get(url, params=params, timeout=15)

        if response.status_code == 401:
            return {"status": "Invalid API Key", "departure": {}, "arrival": {}}
        if response.status_code == 403:
            return {"status": "Access Forbidden", "departure": {}, "arrival": {}}
        if response.status_code == 404:
            return {"status": "Flight Not Found", "departure": {}, "arrival": {}}
        if response.status_code == 429:
            return {"status": "Rate Limit Exceeded", "departure": {}, "arrival": {}}
        if response.status_code != 200:
            return {
                "status": f"API Error {response.status_code}",
                "departure": {},
                "arrival": {},
            }

        data = response.json()

        status, dep_info, arr_info = _extract_status_info(data)
        if not status:
            status = "Not Found"
        else:
            status = status.capitalize()

        return {
            "status": status,
            "departure": dep_info,
            "arrival": arr_info,
        }

    except Exception as e:
        return {
            "status": f"Error: {e}",
            "departure": {},
            "arrival": {},
        }


# ============================================================
# Home Page - Company Login
# ============================================================
@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        company = request.form.get("company", "").strip().lower()

        if not company:
            flash("Please enter a company name.")
            return render_template("index.html")

        if company not in ALLOWED_COMPANIES:
            flash("Access denied.")
            return render_template("index.html")

        return redirect(url_for("flight_status", company=company))

    return render_template("index.html")


# ============================================================
# Flight Status Page
# ============================================================
@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    global last_results

    company = (
        request.args.get("company")
        or request.form.get("company")
        or ""
    ).strip().lower()

    if company not in ALLOWED_COMPANIES:
        flash("Access denied.")
        return redirect(url_for("home"))

    flight_info = None
    uploaded_results = None

    if request.method == "POST":
        airline = request.form.get("airline", "").strip().upper()
        flight_number = request.form.get("flight_number", "").strip()
        departure = request.form.get("departure", "").strip().upper()
        arrival = request.form.get("arrival", "").strip().upper()

        if not all([airline, flight_number, departure, arrival]):
            flash("All fields are required.")
            return render_template(
                "flight_status.html",
                company=company,
                flight_info=None,
                uploaded_results=None,
            )

        # Always assume today
        flight_date = datetime.today().strftime("%Y-%m-%d")

        api_result = fetch_status(airline, flight_number, flight_date)

        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": departure,
            "To": arrival,
            "Status": api_result.get("status", "Unknown"),
            "DepAirport": api_result.get("departure", {}).get("airport"),
            "DepScheduled": api_result.get("departure", {}).get("scheduled"),
            "DepEstimated": api_result.get("departure", {}).get("estimated"),
            "DepTerminalGate": api_result.get("departure", {}).get("terminal_gate"),
            "ArrAirport": api_result.get("arrival", {}).get("airport"),
            "ArrScheduled": api_result.get("arrival", {}).get("scheduled"),
            "ArrEstimated": api_result.get("arrival", {}).get("estimated"),
            "ArrTerminalGate": api_result.get("arrival", {}).get("terminal_gate"),
        }

        last_results = [flight_info]

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results,
    )


# ============================================================
# Excel Upload
# ============================================================
@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results

    company = (
        request.args.get("company")
        or request.form.get("company")
        or ""
    ).strip().lower()

    if company not in ALLOWED_COMPANIES:
        flash("Access denied.")
        return redirect(url_for("home"))

    if "file" not in request.files:
        flash("No file uploaded.")
        return redirect(url_for("flight_status", company=company))

    file = request.files["file"]

    if file.filename == "":
        flash("No file selected.")
        return redirect(url_for("flight_status", company=company))

    try:
        # Load CSV or Excel
        if file.filename.lower().endswith(".csv"):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file)

        df.columns = df.columns.str.strip().str.lower()

        required = ["airline", "flightnumber", "departure", "arrival"]
        missing = [col for col in required if col not in df.columns]

        if missing:
            flash(f"Missing required columns: {missing}")
            return redirect(url_for("flight_status", company=company))

        today = datetime.today().strftime("%Y-%m-%d")

        results = []
        for _, row in df.iterrows():
            airline = str(row["airline"]).strip().upper()
            flight_number = str(row["flightnumber"]).strip()
            departure = str(row["departure"]).strip().upper()
            arrival = str(row["arrival"]).strip().upper()

            api_result = fetch_status(airline, flight_number, today)

            results.append({
                "Airline": airline,
                "FlightNumber": flight_number,
                "From": departure,
                "To": arrival,
                "Status": api_result.get("status", "Unknown"),
                "DepAirport": api_result.get("departure", {}).get("airport"),
                "DepScheduled": api_result.get("departure", {}).get("scheduled"),
                "DepEstimated": api_result.get("departure", {}).get("estimated"),
                "DepTerminalGate": api_result.get("departure", {}).get("terminal_gate"),
                "ArrAirport": api_result.get("arrival", {}).get("airport"),
                "ArrScheduled": api_result.get("arrival", {}).get("scheduled"),
                "ArrEstimated": api_result.get("arrival", {}).get("estimated"),
                "ArrTerminalGate": api_result.get("arrival", {}).get("terminal_gate"),
            })

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


# ============================================================
# Download Excel
# ============================================================
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
        download_name="flight_status_results.xlsx",
        as_attachment=True
    )


# ============================================================
# Run App
# ============================================================
if __name__ == "__main__":
    app.run(debug=True)



