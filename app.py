import os
import json
import pandas as pd
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from datetime import datetime
from io import BytesIO

app = Flask(__name__)
app.secret_key = "your-secret-key"

# --------------------------------------
# Allowed Companies
# --------------------------------------
ALLOWED_COMPANIES_FILE = "allowed_companies.json"

def load_allowed_companies():
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        with open(ALLOWED_COMPANIES_FILE, "r") as f:
            return [c.strip().lower() for c in json.load(f)]
    return []

ALLOWED_COMPANIES = load_allowed_companies()
last_results = []

# --------------------------------------
# FlightAPI.io Configuration
# --------------------------------------
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY", "69175603253bb1627f7ea9cc")
FLIGHTAPI_URL = "https://api.flightapi.io/airline/{key}?num={num}&name={airline}&date={date}"

# --------------------------------------
# Fetch Flight Status (Final Version)
# --------------------------------------
def fetch_status(airline, flight_number, flight_date, departure=None, arrival=None):
    airline_code = airline.lower()
    date_str = flight_date.replace("-", "")

    url = FLIGHTAPI_URL.format(
        key=FLIGHTAPI_KEY,
        num=flight_number,
        airline=airline_code,
        date=date_str,
    )

    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return "Not Found"

        data = resp.json()

        dep = (departure or "").upper()
        arr = (arrival or "").upper()

        status_map = {
            1: "Scheduled",
            2: "Arrived",
            3: "Departed",
            4: "Delayed",
            5: "Cancelled",
        }

        # ----------- Main flights structure -----------
        if isinstance(data, dict) and "flights" in data:
            flights = data.get("flights") or []

            # 1. Exact match: departure + arrival
            for f in flights:
                f_dep = str(f.get("departureAirportCode", "")).upper()
                f_arr = str(f.get("arrivalAirportCode", "")).upper()
                if dep == f_dep and arr == f_arr:
                    return f.get("displayStatus") or status_map.get(f.get("status"), "Unknown")

            # 2. Match by departure airport
            dep_matches = [f for f in flights if str(f.get("departureAirportCode", "")).upper() == dep]
            if dep_matches:
                # Prefer delayed or cancelled
                for f in dep_matches:
                    if f.get("status") in (4, 5):
                        return status_map.get(f.get("status"), "Unknown")
                # Otherwise use the first match
                f = dep_matches[0]
                return f.get("displayStatus") or status_map.get(f.get("status"), "Unknown")

            # 3. Choose the most severe status
            if flights:
                severity = {5: 3, 4: 2, 3: 1, 2: 1, 1: 0}
                flights_sorted = sorted(flights, key=lambda f: severity.get(f.get("status", 0)), reverse=True)
                f = flights_sorted[0]
                return f.get("displayStatus") or status_map.get(f.get("status"), "Unknown")

            return "Not Found"

        # ----------- Simple array response fallback -----------
        if isinstance(data, list):
            for item in reversed(data):
                if isinstance(item, dict) and "status" in item:
                    return item.get("status") or "Unknown"
            return "Not Found"

        return "Not Found"

    except Exception:
        return "Not Found"

# --------------------------------------
# Home (Company Login)
# --------------------------------------
@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        company = request.form.get("company", "").strip().lower()

        if not company:
            flash("Please enter a company name.")
            return render_template("index.html")

        if company not in ALLOWED_COMPANIES:
            flash(f"Access denied for company: {company}")
            return render_template("index.html")

        return redirect(url_for("flight_status", company=company))

    return render_template("index.html")


# --------------------------------------
# Flight Status Page
# --------------------------------------
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

    if request.method == "POST":
        airline = request.form.get("airline", "").strip().upper()
        flight_number = request.form.get("flight_number", "").strip()
        departure = request.form.get("departure", "").strip().upper()
        arrival = request.form.get("arrival", "").strip().upper()

        flight_date = datetime.today().strftime("%Y-%m-%d")

        if not all([airline, flight_number, departure, arrival]):
            flash("All fields are required.")
            return render_template("flight_status.html", company=company)

        status = fetch_status(airline, flight_number, flight_date, departure, arrival)

        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": departure,
            "To": arrival,
            "Status": status,
        }

        last_results = [flight_info]

    return render_template("flight_status.html", company=company, flight_info=flight_info)


# --------------------------------------
# Upload Excel Sheet
# --------------------------------------
@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results

    file = request.files.get("file")
    if not file or file.filename == "":
        flash("No file selected.")
        return redirect(url_for("flight_status"))

    try:
        if file.filename.endswith(".csv"):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file)

        df.columns = df.columns.str.strip().str.lower()

        required = ["airline", "flightnumber", "departure", "arrival"]
        if any(col not in df.columns for col in required):
            flash("Missing required columns: airline, flightnumber, departure, arrival")
            return redirect(url_for("flight_status"))

        today = datetime.today().strftime("%Y-%m-%d")
        results = []

        for _, row in df.iterrows():
            airline = str(row["airline"]).strip().upper()
            flight_number = str(row["flightnumber"]).strip()
            departure = str(row["departure"]).strip().upper()
            arrival = str(row["arrival"]).strip().upper()

            status = fetch_status(airline, flight_number, today, departure, arrival)

            results.append({
                "Airline": airline,
                "FlightNumber": flight_number,
                "From": departure,
                "To": arrival,
                "Status": status,
            })

        last_results = results

        return render_template("flight_status.html", uploaded_results=results)

    except Exception as e:
        flash(f"Error processing file: {e}")
        return redirect(url_for("flight_status"))


# --------------------------------------
# Download Excel
# --------------------------------------
@app.route("/download")
def download_excel():
    if not last_results:
        flash("No results available.")
        return redirect(url_for("home"))

    df = pd.DataFrame(last_results)
    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(output, download_name="flight_status.xlsx", as_attachment=True)


# --------------------------------------
# Run
# --------------------------------------
if __name__ == "__main__":
    app.run(debug=True)
