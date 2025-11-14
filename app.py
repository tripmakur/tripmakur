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
        try:
            with open(ALLOWED_COMPANIES_FILE, "r") as f:
                companies = json.load(f)
                return [c.strip().lower() for c in companies]
        except Exception:
            return []
    return []


ALLOWED_COMPANIES = load_allowed_companies()
last_results = []

# ============================================================
# FlightAPI.io Configuration
# ============================================================

FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY")  # must be set in Render


def parse_flightapi_response(data):
    """
    FlightAPI.io returns a LIST like:
    [
      { "departure": {...} },
      { "arrival": {...} },
      { "aircraft": {...} },
      { "status": "Scheduled" }
    ]

    This helper extracts:
    - status (string)
    - departure info (dict)
    - arrival info (dict)
    """

    status = None
    departure = {}
    arrival = {}

    if not isinstance(data, list):
        return status, departure, arrival

    for item in data:
        if not isinstance(item, dict):
            continue

        if "status" in item and isinstance(item["status"], str):
            status = item["status"]

        if "departure" in item and isinstance(item["departure"], dict):
            departure = item["departure"]

        if "arrival" in item and isinstance(item["arrival"], dict):
            arrival = item["arrival"]

    return status, departure, arrival


def fetch_status(airline, flight_number, flight_date):
    """
    Fetch real flight status from FlightAPI.io using the /airline endpoint.

    Example from FlightAPI.io docs:
    curl -X GET "https://api.flightapi.io/airline/APIKEY?num=4906&name=aa&date=20251114"
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
        "num": flight_number.strip(),
        "name": airline.lower().strip(),
        "date": date_compact,
    }

    try:
        response = requests.get(url, params=params, timeout=15)

        # Basic HTTP error handling
        if response.status_code != 200:
            try:
                msg = response.json().get("message", "")
            except Exception:
                msg = response.text
            return {
                "status": f"API Error {response.status_code}: {msg}",
                "departure": {},
                "arrival": {},
            }

        data = response.json()
        status, dep_raw, arr_raw = parse_flightapi_response(data)

        if not status:
            status = "Not Found"
        else:
            status = status.capitalize()

        # Map departure/arrival info to simple fields expected by UI
        dep_info = {
            "airport": dep_raw.get("airport"),
            "scheduled": dep_raw.get("scheduledTime"),
            "estimated": dep_raw.get("estimatedTime"),
            "terminal_gate": None,
        }
        if dep_raw.get("terminal") or dep_raw.get("gate"):
            dep_info["terminal_gate"] = f"{dep_raw.get('terminal', '')} {dep_raw.get('gate', '')}".strip()

        arr_info = {
            "airport": arr_raw.get("airport"),
            "scheduled": arr_raw.get("scheduledTime"),
            "estimated": arr_raw.get("estimatedTime"),
            "terminal_gate": None,
        }
        if arr_raw.get("terminal") or arr_raw.get("gate"):
            arr_info["terminal_gate"] = f"{arr_raw.get('terminal', '')} {arr_raw.get('gate', '')}".strip()

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

        # Always assume today's date
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




