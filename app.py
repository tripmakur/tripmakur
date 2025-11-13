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
        if not company:
            flash("Please enter a company name.")
            return render_template("index.html")

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

    # Get company: from GET (redirect) or POST (manual/Excel)
    company = request.args.get("company") if request.method == "GET" else request.form.get("company", "")

    if not company or company not in ALLOWED_COMPANIES:
        flash("Access denied or company missing.")
        return redirect(url_for("home"))

    if request.method == "POST" and request.form.get("form_type") == "manual":
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
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results
    )











