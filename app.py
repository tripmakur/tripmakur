import os
import json
import requests
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from datetime import datetime
from io import BytesIO

app = Flask(__name__)
app.secret_key = "your-secret-key"

# ==============================
# Allowed companies
# ==============================
ALLOWED_COMPANIES_FILE = "allowed_companies.json"


def load_allowed_companies():
    """
    Load allowed companies from JSON file.
    If file missing or invalid, treat as "no restriction".
    """
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        try:
            with open(ALLOWED_COMPANIES_FILE, "r") as f:
                data = json.load(f)
                return [str(c).strip().lower() for c in data]
        except Exception:
            # Bad file => no restriction
            return []
    return []  # no file => no restriction


ALLOWED_COMPANIES = load_allowed_companies()
last_results = []


# ==============================
# FlightAPI.io config
# ==============================
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY", "69175603253bb1627f7ea9cc")
FLIGHTAPI_URL = "https://api.flightapi.io/airline/{key}?num={num}&name={airline}&date={date}"


# ==============================
# Fetch status helper
# ==============================
def fetch_status(airline, flight_number, flight_date, departure=None, arrival=None):
    """
    Call FlightAPI.io and return a human-friendly status.

    Handles both:
      - dict with "flights" list
      - raw list with "status" fields

    Also tries to match correct leg using departure + arrival airport codes.
    """
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

        # --- CASE 1: dict with "flights" list (e.g., DL 2388, AA 329) ---
        if isinstance(data, dict) and "flights" in data:
            flights = data.get("flights") or []

            # First try to match by departure + arrival matching the route
            for f in flights:
                dep_code = str(f.get("departureAirportCode", "")).upper()
                arr_code = str(f.get("arrivalAirportCode", "")).upper()

                if dep and arr and dep_code == dep and arr_code == arr:
                    status = f.get("displayStatus") or f.get("status")
                    # status may be numeric – map if needed
                    if isinstance(status, int):
                        numeric_map = {
                            1: "Scheduled",
                            2: "Arrived",
                            3: "Departed",
                            4: "Delayed",
                            5: "Cancelled",
                        }
                        status = numeric_map.get(status, "Unknown")
                    return status or "Unknown"

            # If no exact route match, fall back to first flight
            if flights:
                first = flights[0]
                status = first.get("displayStatus") or first.get("status")
                if isinstance(status, int):
                    numeric_map = {
                        1: "Scheduled",
                        2: "Arrived",
                        3: "Departed",
                        4: "Delayed",
                        5: "Cancelled",
                    }
                    status = numeric_map.get(status, "Unknown")
                return status or "Unknown"

            return "Not Found"

        # --- CASE 2: array-style response: [{...}, {...}, {"status": "Arrived"}] ---
        if isinstance(data, list):
            # Search from the end for an element with "status"
            for item in reversed(data):
                if isinstance(item, dict) and "status" in item:
                    return item["status"] or "Unknown"
            return "Not Found"

        return "Not Found"

    except Exception:
        return "Not Found"


# ==============================
# Home (company gate)
# ==============================
@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        company = request.form.get("company", "").strip().lower()

        if not company:
            flash("Please enter your company name.")
            return render_template("index.html")

        # Only enforce if we actually have a restriction list
        if ALLOWED_COMPANIES and company not in ALLOWED_COMPANIES:
            flash("Access denied: unauthorized company.")
            return render_template("index.html")

        return redirect(url_for("flight_status", company=company))

    return render_template("index.html")


# ==============================
# Flight status page
# ==============================
@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    global last_results

    company = (request.args.get("company") or request.form.get("company") or "").strip().lower()

    # Only enforce if a restriction list exists
    if ALLOWED_COMPANIES and company not in ALLOWED_COMPANIES:
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

        today = datetime.today().strftime("%Y-%m-%d")
        status = fetch_status(airline, flight_number, today, departure, arrival)

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


# ==============================
# Excel upload
# ==============================
@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results

    company = request.form.get("company", "").strip().lower()

    # Only enforce restrictions if configured
    if ALLOWED_COMPANIES and company and company not in ALLOWED_COMPANIES:
        flash("Access denied.")
        return redirect(url_for("home"))

    if "file" not in request.files:
        flash("No file uploaded.")
        return redirect(url_for("flight_status", company=company))

    file = request.files["file"]
    if not file.filename:
        flash("No selected file.")
        return redirect(url_for("flight_status", company=company))

    try:
        # Use first row as header
        if file.filename.lower().endswith(".csv"):
            df = pd.read_csv(file, header=0)
        else:
            df = pd.read_excel(file, header=0)

        # Ensure headers are strings, then normalize
        df.columns = df.columns.astype(str)
        df.columns = df.columns.str.strip().str.lower()

        # We expect *lowercase* matching your sheet:
        # airline, flightnumber, departure, arrival
        required = ["airline", "flightnumber", "departure", "arrival"]
        missing = [c for c in required if c not in df.columns]

        if missing:
            flash(f"Missing required columns: {missing}")
            return redirect(url_for("flight_status", company=company))

        today = datetime.today().strftime("%Y-%m-%d")
        results = []

        for _, row in df.iterrows():
            airline = str(row.get("airline", "")).strip().upper()
            flight_number = str(row.get("flightnumber", "")).strip()
            departure = str(row.get("departure", "")).strip().upper()
            arrival = str(row.get("arrival", "")).strip().upper()

            if not airline or not flight_number:
                results.append({
                    "Airline": airline,
                    "FlightNumber": flight_number,
                    "From": departure,
                    "To": arrival,
                    "Status": "Missing Data",
                })
                continue

            status = fetch_status(airline, flight_number, today, departure, arrival)

            results.append({
                "Airline": airline,
                "FlightNumber": flight_number,
                "From": departure,
                "To": arrival,
                "Status": status,
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


# ==============================
# Download Excel
# ==============================
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
        as_attachment=True,
    )


if __name__ == "__main__":
    app.run(debug=True)



if __name__ == "__main__":
    app.run(debug=True)
