import os
import json
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file
import pandas as pd
from datetime import datetime
from io import BytesIO

app = Flask(__name__)
app.secret_key = "your-secret-key"

# -----------------------------------------
# Load allowed companies
# -----------------------------------------
ALLOWED_COMPANIES_FILE = "allowed_companies.json"

def load_allowed_companies():
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        with open(ALLOWED_COMPANIES_FILE, "r") as f:
            return [c.strip().lower() for c in json.load(f)]
    return []

ALLOWED_COMPANIES = load_allowed_companies()
last_results = []

# -----------------------------------------
# Airline mapping required by FlightAPI.io
# -----------------------------------------
AIRLINE_NAME_MAP = {
    "AA": "aa",
    "DL": "delta",
    "UA": "ua",
    "WN": "southwest",
    "B6": "jetblue",
    "AS": "alaska",
    "NK": "spirit",
    "F9": "frontier",
    "SY": "suncountry",
    "HA": "hawaiian"
}

# -----------------------------------------
# FLIGHTAPI.IO KEY
# -----------------------------------------
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY", "69175603253bb1627f7ea9cc")


# -----------------------------------------
# Fetch Flight Status — Corrected for /airline/ endpoint
# -----------------------------------------
def fetch_status(airline, flight_number, flight_date):
    airline_name = AIRLINE_NAME_MAP.get(airline.upper(), airline.lower())
    date_str = flight_date.replace("-", "")

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline_name}&date={date_str}"
    )

    try:
        resp = requests.get(url)
        data = resp.json()

        # Must be a list (array) per FlightAPI.io format
        if isinstance(data, list):
            # Look for the object containing "status"
            for item in data:
                if isinstance(item, dict) and "status" in item:
                    return item["status"]

            # If no status found
            return "Status Not Available"

        return "Not Found"

    except Exception as e:
        return f"API Error: {e}"


# -----------------------------------------
# HOME PAGE — Company Login
# -----------------------------------------
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


# -----------------------------------------
# FLIGHT STATUS PAGE
# -----------------------------------------
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


# -----------------------------------------
# EXCEL UPLOAD
# -----------------------------------------
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


# -----------------------------------------
# DOWNLOAD EXCEL
# -----------------------------------------
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


# -----------------------------------------
if __name__ == "__main__":
    app.run(debug=True)



# -----------------------------------------
if __name__ == "__main__":
    app.run(debug=True)
