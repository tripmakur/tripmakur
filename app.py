import os
import json
import requests
import pandas as pd
from datetime import datetime
from io import BytesIO
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, send_file
)

app = Flask(__name__)
app.secret_key = "your-secret-key"

# ============================
# AviationStack API
# ============================
AVIATIONSTACK_API_KEY = "c60cdc6d78336192505479f9252862c5"


def fetch_status(airline, flight_number, flight_date):
    """
    Fetch real flight status using AviationStack API.
    """
    base_url = "http://api.aviationstack.com/v1/flights"

    params = {
        "access_key": AVIATIONSTACK_API_KEY,
        "flight_iata": f"{airline}{flight_number}",
        "flight_date": flight_date
    }

    try:
        response = requests.get(base_url, params=params)
        data = response.json()

        # API error handling
        if "error" in data:
            return f"API Error: {data['error'].get('message', 'Unknown error')}"

        flights = data.get("data", [])
        if not flights:
            return "Not Found"

        status = flights[0].get("flight_status", "Unknown").title()
        return status

    except Exception as e:
        return f"Error: {e}"


# ============================
# Allowed Company System
# ============================
ALLOWED_COMPANIES_FILE = "allowed_companies.json"


def load_allowed_companies():
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        with open(ALLOWED_COMPANIES_FILE, "r") as f:
            try:
                companies = json.load(f)
                return [c.strip().lower() for c in companies]
            except:
                return []
    return []


ALLOWED_COMPANIES = load_allowed_companies()
last_results = []


# ============================
# HOME PAGE (Enter Company)
# ============================
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

        # Redirect to flight status dashboard
        return redirect(url_for("flight_status", company=company))

    return render_template("index.html")


# ============================
# FLIGHT STATUS PAGE
# ============================
@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    global last_results

    company = (
        request.args.get("company")
        or request.form.get("company")
        or ""
    ).strip().lower()

    if not company or company not in ALLOWED_COMPANIES:
        flash("Access denied.")
        return redirect(url_for("home"))

    flight_info = None
    uploaded_results = None

    if request.method == "POST":
        airline = request.form.get("airline", "").strip().upper()
        flight_number = request.form.get("flight_number", "").strip()
        departure = request.form.get("departure", "").strip().upper()
        arrival = request.form.get("arrival", "").strip().upper()

        # Assume today's date always
        flight_date = datetime.today().strftime("%Y-%m-%d")

        if not all([airline, flight_number, departure, arrival]):
            flash("All fields are required.")
            return render_template("flight_status.html",
                                   company=company,
                                   flight_info=None,
                                   uploaded_results=None)

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


# ============================
# EXCEL UPLOAD HANDLER
# ============================
@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results

    if "file" not in request.files:
        flash("No file uploaded.")
        return redirect(url_for("flight_status"))

    file = request.files["file"]
    if file.filename == "":
        flash("No selected file.")
        return redirect(url_for("flight_status"))

    try:
        # Accept CSV or Excel
        if file.filename.endswith(".csv"):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file, sheet_name=0)

        # Normalize column names
        df.columns = df.columns.str.strip().str.lower()

        required_cols = ["airline", "flightnumber", "departure", "arrival"]
        missing = [c for c in required_cols if c not in df.columns]

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
                "Status": status
            })

        last_results = results

        return render_template(
            "flight_status.html",
            flight_info=None,
            uploaded_results=results,
            company=request.args.get("company", "")
        )

    except Exception as e:
        flash(f"Error processing file: {e}")
        return redirect(url_for("flight_status"))


# ============================
# DOWNLOAD RESULTS AS EXCEL
# ============================
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


# ============================
# RUN APP
# ============================
if __name__ == "__main__":
    app.run(debug=True)

















