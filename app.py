import os
import json
import requests
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from datetime import datetime
from io import BytesIO

app = Flask(__name__)
app.secret_key = "your-secret-key"

# ================================
# Allowed Companies
# ================================
ALLOWED_COMPANIES_FILE = "allowed_companies.json"


def load_allowed_companies():
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        with open(ALLOWED_COMPANIES_FILE, "r") as f:
            companies = json.load(f)
            return [c.strip().lower() for c in companies]
    return []


ALLOWED_COMPANIES = load_allowed_companies()
last_results = []


# ================================
# FlightAPI.io Lookup
# ================================
FLIGHTAPI_KEY = "69175603253bb1627f7ea9cc"


def fetch_status_flightapi(airline, flight_number, flight_date):
    """
    Fetch status using FlightAPI.io
    """

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline}&date={flight_date}"
    )

    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            return f"API Error ({resp.status_code})"

        data = resp.json()

        # If FlightAPI returns list form (like AA 329 case)
        if isinstance(data, list):
            for entry in data:
                if "status" in entry:
                    return entry["status"]
            return "Not Found"

        # If FlightAPI returns dictionary form
        if isinstance(data, dict) and "flights" in data:
            flights = data.get("flights", [])
            if not flights:
                return "Not Found"

            # Take the FIRST flight (most recent)
            status = flights[0].get("displayStatus", "Not Found")
            return status

        return "Not Found"

    except Exception as e:
        return f"Error: {str(e)}"


# ================================
# HOME PAGE
# ================================
@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        company = request.form.get("company", "").strip().lower()

        if not company:
            flash("Please enter a company name.")
            return render_template("index.html")

        if company not in ALLOWED_COMPANIES:
            flash("Access denied for company: " + company)
            return render_template("index.html")

        return redirect(url_for("flight_status", company=company))

    return render_template("index.html")


# ================================
# MANUAL FLIGHT LOOKUP
# ================================
@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    global last_results

    company = (request.args.get("company") or request.form.get("company") or "").lower()

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
            return render_template("flight_status.html", company=company)

        # Always use today's date
        flight_date = datetime.today().strftime("%Y%m%d")

        status = fetch_status_flightapi(airline, flight_number, flight_date)

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


# ================================
# EXCEL UPLOAD
# ================================
@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results

    file = request.files.get("file")
    if not file or file.filename == "":
        flash("No file uploaded.")
        return redirect(url_for("flight_status"))

    try:
        # Support CSV or Excel
        if file.filename.endswith(".csv"):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file)

        df.columns = df.columns.str.strip().str.lower()

        required = ["airline", "flightnumber", "departure", "arrival"]
        for col in required:
            if col not in df.columns:
                raise ValueError(f"Missing required column: {col}")

        today = datetime.today().strftime("%Y%m%d")
        results = []

        for _, row in df.iterrows():
            airline = str(row["airline"]).strip().upper()
            flight_number = str(row["flightnumber"]).strip()
            dep = str(row["departure"]).strip().upper()
            arr = str(row["arrival"]).strip().upper()

            status = fetch_status_flightapi(airline, flight_number, today)

            results.append({
                "Airline": airline,
                "FlightNumber": flight_number,
                "From": dep,
                "To": arr,
                "Status": status,
            })

        last_results = results

        return render_template(
            "flight_status.html",
            company=request.args.get("company", ""),
            uploaded_results=results
        )

    except Exception as e:
        flash(f"Error processing file: {e}")
        return redirect(url_for("flight_status"))


# ================================
# DOWNLOAD RESULTS
# ================================
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
