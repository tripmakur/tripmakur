from flask import Flask, render_template, request, flash, redirect, url_for, send_file
import pandas as pd
from io import BytesIO
from datetime import datetime
import json
import os
import requests

app = Flask(__name__)
app.secret_key = "your_secret_key"

last_results = []

# -----------------------
# Allowed companies
# -----------------------
ALLOWED_COMPANIES_FILE = "allowed_companies.json"

def load_allowed_companies():
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        with open(ALLOWED_COMPANIES_FILE, "r") as f:
            return json.load(f)
    return []

ALLOWED_COMPANIES = load_allowed_companies()

# -----------------------
# AviationStack API helper
# -----------------------
API_KEY = "YOUR_AVIATIONSTACK_API_KEY"

def fetch_status(airline_code, flight_number, flight_date=None):
    try:
        if not flight_date:
            flight_date = datetime.today().strftime("%Y-%m-%d")
        flight_iata = f"{airline_code}{flight_number}"
        params = {"access_key": API_KEY, "flight_iata": flight_iata, "flight_date": flight_date}
        response = requests.get("http://api.aviationstack.com/v1/flights", params=params)
        data = response.json()
        if "data" not in data or not data["data"]:
            return "Not Found"
        flight = data["data"][0]
        return flight.get("flight_status", "Not Found")
    except Exception as e:
        return f"Error: {e}"

# -----------------------
# Home page
# -----------------------
@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        company = request.form.get("company", "").strip()
        if company not in ALLOWED_COMPANIES:
            flash(f"Access denied for company: {company}")
            return render_template("index.html")
        # Redirect to flight-status page with company in query string
        return redirect(url_for("flight_status", company=company))
    return render_template("index.html")

# -----------------------
# Flight status page
# -----------------------
@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    global last_results
    flight_info = None
    uploaded_results = None

    # Safely get company: from GET query on redirect, or POST form
    if request.method == "GET":
        company = request.args.get("company", "")
    else:
        company = request.form.get("company", "")

    if company not in ALLOWED_COMPANIES:
        flash(f"Access denied for company: {company}")
        return redirect(url_for("home"))

    if request.method == "POST" and request.form.get("form_type") == "manual":
        # Manual flight lookup
        airline = request.form.get("airline", "").strip().upper()
        flight_number = request.form.get("flight_number", "").strip()
        departure = request.form.get("departure", "").strip().upper()
        arrival = request.form.get("arrival", "").strip().upper()
        flight_date = request.form.get("flight_date", "").strip()

        if not all([airline, flight_number, departure, arrival]):
            flash("All fields are required.")
        else:
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

    return render_template(
        "flight_status.html",
        flight_info=flight_info,
        uploaded_results=uploaded_results,
        company=company
    )

# -----------------------
# Excel upload
# -----------------------
@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results
    company = request.form.get("company", "").strip()
    if company not in ALLOWED_COMPANIES:
        flash(f"Access denied for company: {company}")
        return redirect(url_for("home"))

    if "file" not in request.files:
        flash("No file part in request.")
        return redirect(url_for("flight_status", company=company))

    file = request.files["file"]
    if file.filename == "":
        flash("No file selected.")
        return redirect(url_for("flight_status", company=company))

    try:
        if file.filename.lower().endswith(".csv"):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file)

        # Normalize columns
        column_mapping = {
            "airline": "Airline",
            "flightnumber": "FlightNumber",
            "from": "From",
            "departure": "From",
            "to": "To",
            "arrival": "To",
            "date": "Date"
        }
        normalized = {}
        for col in df.columns:
            col_lower = col.strip().lower()
            if col_lower in column_mapping:
                normalized[column_mapping[col_lower]] = df[col]

        df = pd.DataFrame(normalized)
        required = {"Airline", "FlightNumber", "From", "To"}
        missing = required - set(df.columns)
        if missing:
            flash(f"Missing required columns: {', '.join(missing)}")
            return redirect(url_for("flight_status", company=company))

    except Exception as e:
        flash(f"Error reading file: {e}")
        return redirect(url_for("flight_status", company=company))

    # Process flights
    results = []
    for _, row in df.iterrows():
        airline = str(row["Airline"]).strip().upper()
        flight_number = str(row["FlightNumber"]).strip()
        departure = str(row["From"]).strip().upper()
        arrival = str(row["To"]).strip().upper()
        flight_date = str(row["Date"]).strip() if "Date" in row and row["Date"] else datetime.today().strftime("%Y-%m-%d")
        status = fetch_status(airline, flight_number, flight_date)
        results.append({
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": departure,
            "To": arrival,
            "Status": status
        })

    last_results = results
    return render_template("flight_status.html", uploaded_results=results, flight_info=None, company=company)

# -----------------------
# Download results as Excel
# -----------------------
@app.route("/download-excel")
def download_excel():
    if not last_results:
        flash("No results to download.")
        return redirect(url_for("home"))

    df = pd.DataFrame(last_results)
    output = BytesIO()
    df.to_excel(output, index=False, sheet_name="FlightStatus")
    output.seek(0)
    return send_file(output, as_attachment=True,
                     download_name="flight_status_results.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")










