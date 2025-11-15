import os
import json
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file
import pandas as pd
from datetime import datetime
from io import BytesIO

app = Flask(__name__)
app.secret_key = "your-secret-key"

# ------------------------------
# Allowed Companies
# ------------------------------
ALLOWED_COMPANIES_FILE = "allowed_companies.json"


def load_allowed_companies():
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        try:
            with open(ALLOWED_COMPANIES_FILE, "r") as f:
                data = json.load(f)
                return [c.strip().lower() for c in data]
        except:
            return []
    return []


ALLOWED_COMPANIES = load_allowed_companies()
last_results = []  # stored results for Excel download

# ------------------------------
# FlightAPI.io Settings
# ------------------------------
FLIGHTAPI_KEY = os.environ.get("FLIGHTAPI_KEY", "YOUR_KEY_HERE")

# Example format:
# https://api.flightapi.io/airline/{API_KEY}?num=329&name=AA&date=20251114
FLIGHTAPI_URL = (
    "https://api.flightapi.io/airline/{key}?num={num}&name={airline}&date={date}"
)

# ------------------------------
# Flight Status Fetcher
# ------------------------------
def fetch_status(airline, flight_number, flight_date, departure=None, arrival=None):
    """Fetch status from FlightAPI.io with route matching and fallback."""
    
    airline_code = airline.upper()  # IMPORTANT: MUST BE UPPERCASE
    date_str = flight_date.replace("-", "")

    url = FLIGHTAPI_URL.format(
        key=FLIGHTAPI_KEY,
        num=flight_number,
        airline=airline_code,
        date=date_str,
    )

    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            return "Not Found"

        data = resp.json()
        dep = (departure or "").upper()
        arr = (arrival or "").upper()

        # ------------------------------
        # Case 1: FlightAPI main format
        # data = {"flights":[...]}
        # ------------------------------
        if isinstance(data, dict) and "flights" in data:
            flights = data.get("flights") or []

            status_map = {
                1: "Scheduled",
                2: "Arrived",
                3: "Departed",
                4: "Delayed",
                5: "Cancelled",
            }

            # 1. Exact Route Match
            for f in flights:
                f_dep = str(f.get("departureAirportCode", "")).upper()
                f_arr = str(f.get("arrivalAirportCode", "")).upper()

                if dep == f_dep and arr == f_arr:
                    return f.get("displayStatus") or status_map.get(f.get("status"))

            # 2. Match by departure only
            dep_matches = [
                f for f in flights if str(f.get("departureAirportCode", "")).upper() == dep
            ]
            if dep_matches:
                # Prefer Delayed or Cancelled
                for f in dep_matches:
                    if f.get("status") in (4, 5):
                        return status_map.get(f.get("status"), "Unknown")

                # Otherwise return first match
                f = dep_matches[0]
                return f.get("displayStatus") or status_map.get(f.get("status"))

            # 3. Fallback: highest severity status
            if flights:
                severity_order = {5: 3, 4: 2, 3: 1, 2: 1, 1: 0}
                flights_sorted = sorted(
                    flights,
                    key=lambda x: severity_order.get(x.get("status", 0)),
                    reverse=True,
                )
                f = flights_sorted[0]
                return f.get("displayStatus") or status_map.get(f.get("status"))

            return "Not Found"

        # ------------------------------
        # Case 2: Simple list
        # data = [ {departure...}, {arrival...}, {status: "..."} ]
        # ------------------------------
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "status" in item:
                    return item["status"]

            return "Not Found"

        return "Not Found"

    except Exception:
        return "Not Found"


# ------------------------------
# HOME PAGE (Company Login)
# ------------------------------
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

        # Pass company name to status page
        return redirect(url_for("flight_status", company=company))

    return render_template("index.html")


# ------------------------------
# FLIGHT STATUS PAGE
# ------------------------------
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
        flight_date = datetime.today().strftime("%Y-%m-%d")  # always today

        if not all([airline, flight_number, departure, arrival]):
            flash("All fields are required.")
            return render_template(
                "flight_status.html",
                company=company
            )

        status = fetch_status(
            airline,
            flight_number,
            flight_date,
            departure=departure,
            arrival=arrival,
        )

        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": departure,
            "To": arrival,
            "Status": status,
        }

        last_results = [flight_info]

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results,
    )


# ------------------------------
# EXCEL UPLOAD
# ------------------------------
@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results

    if "file" not in request.files:
        flash("No file uploaded.")
        return redirect(url_for("flight_status"))

    file = request.files["file"]
    if not file.filename:
        flash("No file selected.")
        return redirect(url_for("flight_status"))

    try:
        if file.filename.endswith(".csv"):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file, sheet_name=0)

        # Normalize
        df.columns = df.columns.str.strip().str.lower()

        required = ["airline", "flightnumber", "departure", "arrival"]
        for col in required:
            if col not in df.columns:
                raise KeyError(f"Missing column: {col}")

        today = datetime.today().strftime("%Y-%m-%d")
        results = []

        for _, row in df.iterrows():
            airline = str(row["airline"]).strip().upper()
            flight_number = str(row["flightnumber"]).strip()
            departure = str(row["departure"]).strip().upper()
            arrival = str(row["arrival"]).strip().upper()

            status = fetch_status(
                airline,
                flight_number,
                today,
                departure=departure,
                arrival=arrival,
            )

            results.append(
                {
                    "Airline": airline,
                    "FlightNumber": flight_number,
                    "From": departure,
                    "To": arrival,
                    "Status": status,
                }
            )

        last_results = results

        return render_template(
            "flight_status.html",
            uploaded_results=results
        )

    except Exception as e:
        flash(f"Error processing file: {e}")
        return redirect(url_for("flight_status"))


# ------------------------------
# DOWNLOAD EXCEL
# ------------------------------
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
        as_attachment=True,
    )


# ------------------------------
# RUN APP
# ------------------------------
if __name__ == "__main__":
    app.run(debug=True)
