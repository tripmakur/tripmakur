import os
import json
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file
import pandas as pd
from datetime import datetime
from io import BytesIO

app = Flask(__name__)
app.secret_key = "your-secret-key"

# ============================================================
# Load allowed companies
# ============================================================
ALLOWED_COMPANIES_FILE = "allowed_companies.json"

def load_allowed_companies():
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        with open(ALLOWED_COMPANIES_FILE, "r") as f:
            return [c.strip().lower() for c in json.load(f)]
    return []

ALLOWED_COMPANIES = load_allowed_companies()
last_results = []

# ============================================================
# FLIGHTAPI.IO KEY (Render environment variable)
# ============================================================
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY", "69175603253bb1627f7ea9cc")


# ============================================================
# Fetch flight status — Correct for your API format
# ============================================================
def fetch_status(airline, flight_number, flight_date):
    """
    Correct parser for FlightAPI.io /airline/ endpoint:
    https://api.flightapi.io/airline/{KEY}?num=2388&name=dl&date=20251114
    """

    # airline must be lowercase IATA code (aa, dl, ua…)
    airline_name = airline.lower()
    date_str = flight_date.replace("-", "")

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline_name}&date={date_str}"
    )

    try:
        response = requests.get(url)
        data = response.json()

        # Expected format:
        # { "flights": [ {...}, {...} ], "emptyResults": false }
        if isinstance(data, dict) and "flights" in data and data["flights"]:

            flights = data["flights"]

            # Status priority
            priority = {
                "Cancelled": 5,
                "Delayed": 4,
                "Departed": 3,
                "Arrived": 2,
                "Scheduled": 1
            }

            best_status = None
            best_score = 0

            for f in flights:
                status = f.get("displayStatus") or f.get("status")

                if not status:
                    continue

                # Convert numeric status if needed
                if isinstance(status, int):
                    # Status mapping (based on FlightAPI.io)
                    numeric_map = {
                        1: "Scheduled",
                        2: "Arrived",
                        3: "Departed",
                        4: "Delayed",
                        5: "Cancelled"
                    }
                    status = numeric_map.get(status, None)

                if status in priority:
                    if priority[status] > best_score:
                        best_score = priority[status]
                        best_status = status

            return best_status if best_status else "Unknown Status"

        return "Not Found"

    except Exception as e:
        return f"API Error: {e}"


# ============================================================
# HOME PAGE — Company Login
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
# FLIGHT STATUS PAGE
# ============================================================
@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    global last_results

    company = (request.args.get("company") or request.form.get("company") or "").strip().lower()

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
        flight_date = request.form.get("flight_date", "").strip()

        if not all([airline, flight_number, departure, arrival]):
            flash("All fields are required.")
            return render_template("flight_status.html",
                                   company=company,
                                   flight_info=None,
                                   uploaded_results=None)

        if not flight_date:
            flight_date = datetime.today().strftime("%Y-%m-%d")

        status = fetch_status(airline, flight_number, flight_date)

        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": departure,
            "To": arrival,
            "Status": status,
        }

        last_results = [flight_info]

    return render_template("flight_status.html",
                           company=company,
                           flight_info=flight_info,
                           uploaded_results=uploaded_results)


# ============================================================
# EXCEL UPLOAD
# ============================================================
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

        df.columns = df.columns.map(lambda x: str(x).strip().lower())

        required = ["airline", "flightnumber", "departure", "arrival"]
        missing = [c for c in required if c not in df.columns]

        if missing:
            flash(f"Missing required columns: {missing}")
            return redirect(url_for("flight_status"))

        today = datetime.today().strftime("%Y-%m-%d")
        results = []

        for _, row in df.iterrows():
            airline = str(row["airline"]).strip().upper()
            flight_number = str(row["flightnumber"]).strip()
            departure = str(row["departure"]).strip().upper()
            arrival = str(row["arrival"]).strip().upper()

            status = fetch_status(airline, flight_number, today)

            results.append({
                "Airline": airline,
                "FlightNumber": flight_number,
                "From": departure,
                "To": arrival,
                "Status": status,
            })

        last_results = results

        return render_template("flight_status.html",
                               company=request.form.get("company"),
                               flight_info=None,
                               uploaded_results=results)

    except Exception as e:
        flash(f"Error processing file: {e}")
        return redirect(url_for("flight_status"))


# ============================================================
# DOWNLOAD EXCEL
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

    return send_file(output,
                     download_name="flight_results.xlsx",
                     as_attachment=True)


# ============================================================
if __name__ == "__main__":
    app.run(debug=True)

