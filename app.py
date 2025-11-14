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
                return [c.strip().lower() for c in json.load(f)]
        except:
            return []
    return []


ALLOWED_COMPANIES = load_allowed_companies()
last_results = []

# ============================================================
# FlightAPI.io Config
# ============================================================

FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY")


# ============================================================
# PARSER HELPERS
# ============================================================

def parse_airline_endpoint(data):
    """
    Parse the /airline endpoint:
    [
      {"departure": {...}},
      {"arrival": {...}},
      {"aircraft": {...}},
      {"status": "Departed"}
    ]
    """
    if not isinstance(data, list):
        return None, {}, {}

    status = None
    dep = {}
    arr = {}

    for item in data:
        if not isinstance(item, dict):
            continue
        if "status" in item:
            status = item["status"]
        if "departure" in item:
            dep = item["departure"]
        if "arrival" in item:
            arr = item["arrival"]

    return status, dep, arr


def parse_flights_endpoint(data):
    """
    Parse the /flights endpoint:
    {
      "flights": [
         { "displayStatus": "...", "departureTime": "...", ... }
      ]
    }
    """
    flights = data.get("flights", [])
    if not flights:
        return None, {}, {}

    f = flights[0]  # first flight is the relevant one

    status = f.get("displayStatus")

    dep = {
        "airport": f.get("departureAirportName"),
        "scheduled": f.get("departureTime"),
        "estimated": f.get("departureTime"),
        "terminal_gate": None,
    }

    arr = {
        "airport": f.get("arrivalAirportName"),
        "scheduled": f.get("arrivalTime"),
        "estimated": f.get("arrivalTime"),
        "terminal_gate": None,
    }

    return status, dep, arr


# ============================================================
# FlightAPI.io Fetch with dual-endpoint fallback
# ============================================================

def fetch_status(airline, flight_number, flight_date):
    """
    1) Try /airline endpoint
    2) If empty, fallback to /flights endpoint
    """

    if not FLIGHTAPI_KEY:
        return {
            "status": "API Key Missing",
            "departure": {},
            "arrival": {}
        }

    date_compact = flight_date.replace("-", "")

    # --------------------------------------------------------
    # TRY #1 — /airline endpoint
    # --------------------------------------------------------
    url1 = f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
    params1 = {
        "name": airline.lower().strip(),
        "num": flight_number.strip(),
        "date": date_compact
    }

    try:
        r1 = requests.get(url1, params=params1, timeout=10)

        if r1.status_code == 200:
            data = r1.json()

            status, dep_raw, arr_raw = parse_airline_endpoint(data)

            if status:  # valid info
                return {
                    "status": status.capitalize(),
                    "departure": {
                        "airport": dep_raw.get("airport"),
                        "scheduled": dep_raw.get("scheduledTime"),
                        "estimated": dep_raw.get("estimatedTime"),
                        "terminal_gate":
                            f"{dep_raw.get('terminal','')} {dep_raw.get('gate','')}".strip()
                            if dep_raw.get("terminal") or dep_raw.get("gate") else None
                    },
                    "arrival": {
                        "airport": arr_raw.get("airport"),
                        "scheduled": arr_raw.get("scheduledTime"),
                        "estimated": arr_raw.get("estimatedTime"),
                        "terminal_gate":
                            f"{arr_raw.get('terminal','')} {arr_raw.get('gate','')}".strip()
                            if arr_raw.get("terminal") or arr_raw.get("gate") else None
                    }
                }
    except Exception:
        pass

    # --------------------------------------------------------
    # TRY #2 — /flights endpoint (RELIABLE)
    # --------------------------------------------------------
    url2 = f"https://api.flightapi.io/flights/{FLIGHTAPI_KEY}"
    params2 = {
        "flight": f"{airline.upper()}{flight_number}",
        "date": date_compact
    }

    try:
        r2 = requests.get(url2, params=params2, timeout=10)

        if r2.status_code == 200:
            data = r2.json()

            status, dep_raw, arr_raw = parse_flights_endpoint(data)

            if status:
                return {
                    "status": status,
                    "departure": dep_raw,
                    "arrival": arr_raw
                }

    except Exception as e:
        return {"status": f"Error: {e}", "departure": {}, "arrival": {}}

    return {"status": "Not Found", "departure": {}, "arrival": {}}


# ============================================================
# Home (company login)
# ============================================================

@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        company = request.form.get("company", "").strip().lower()

        if company in ALLOWED_COMPANIES:
            return redirect(url_for("flight_status", company=company))

        flash("Access denied.")
        return render_template("index.html")

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
            flash("Please fill all fields.")
            return render_template("flight_status.html", company=company)

        flight_date = datetime.today().strftime("%Y-%m-%d")

        api_result = fetch_status(airline, flight_number, flight_date)

        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": departure,
            "To": arrival,
            "Status": api_result["status"],
            "DepAirport": api_result["departure"].get("airport"),
            "DepScheduled": api_result["departure"].get("scheduled"),
            "DepEstimated": api_result["departure"].get("estimated"),
            "DepTerminalGate": api_result["departure"].get("terminal_gate"),
            "ArrAirport": api_result["arrival"].get("airport"),
            "ArrScheduled": api_result["arrival"].get("scheduled"),
            "ArrEstimated": api_result["arrival"].get("estimated"),
            "ArrTerminalGate": api_result["arrival"].get("terminal_gate"),
        }

        last_results = [flight_info]

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results
    )


# ============================================================
# Spreadsheet Upload
# ============================================================

@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results

    company = request.args.get("company", "").strip().lower()
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
        if file.filename.endswith(".csv"):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file)

        df.columns = df.columns.str.lower().str.strip()

        required = ["airline", "flightnumber", "departure", "arrival"]
        missing = [c for c in required if c not in df.columns]

        if missing:
            flash(f"Missing columns: {missing}")
            return redirect(url_for("flight_status", company=company))

        today = datetime.today().strftime("%Y-%m-%d")
        results = []

        for _, row in df.iterrows():
            airline = str(row["airline"]).strip().upper()
            fn = str(row["flightnumber"]).strip()
            dep = str(row["departure"]).strip().upper()
            arr = str(row["arrival"]).strip().upper()

            api_result = fetch_status(airline, fn, today)

            results.append({
                "Airline": airline,
                "FlightNumber": fn,
                "From": dep,
                "To": arr,
                "Status": api_result["status"],
                "DepAirport": api_result["departure"].get("airport"),
                "DepScheduled": api_result["departure"].get("scheduled"),
                "DepEstimated": api_result["departure"].get("estimated"),
                "DepTerminalGate": api_result["departure"].get("terminal_gate"),
                "ArrAirport": api_result["arrival"].get("airport"),
                "ArrScheduled": api_result["arrival"].get("scheduled"),
                "ArrEstimated": api_result["arrival"].get("estimated"),
                "ArrTerminalGate": api_result["arrival"].get("terminal_gate"),
            })

        last_results = results

        return render_template(
            "flight_status.html",
            company=company,
            flight_info=None,
            uploaded_results=results
        )

    except Exception as e:
        flash(f"Error reading file: {e}")
        return redirect(url_for("flight_status", company=company))


# ============================================================
# Download Excel
# ============================================================

@app.route("/download")
def download_excel():
    if not last_results:
        flash("No results available.")
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





