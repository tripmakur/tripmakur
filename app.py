import os
import json
import time
from datetime import datetime
from io import BytesIO

import pandas as pd
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "your-secret-key")

# ============================================================
# Allowed Companies
# ============================================================
ALLOWED_COMPANIES_FILE = "allowed_companies.json"

def load_allowed_companies():
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        try:
            with open(ALLOWED_COMPANIES_FILE, "r") as f:
                companies = json.load(f)
                return [str(c).strip().lower() for c in companies]
        except:
            return []
    return []

ALLOWED_COMPANIES = load_allowed_companies()
last_results = []
last_analysis_results = []


def is_company_allowed(company: str) -> bool:
    if not ALLOWED_COMPANIES:
        return True
    if not company:
        return False
    return company.lower() in ALLOWED_COMPANIES


# ============================================================
# FlightAPI — Flight Status Logic
# ============================================================
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY")

def fetch_status_flightapi(airline: str, flight_number: str) -> dict:
    """
    Returns:
    {
        "Status": "...",
        "EstimatedDeparture": "...",
        "EstimatedArrival": "..."
    }
    """
    airline_code = airline.upper()
    today = datetime.utcnow().strftime("%Y%m%d")

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline_code}&date={today}"
    )

    try:
        resp = requests.get(url, timeout=20)

        if resp.status_code != 200:
            return {"Status": f"API Error", "EstimatedDeparture": None, "EstimatedArrival": None}

        data = resp.json()

        if "flights" not in data or not data["flights"]:
            return {"Status": "Not Found", "EstimatedDeparture": None, "EstimatedArrival": None}

        flight = data["flights"][0]

        # Extract status
        raw_status = flight.get("displayStatus") or flight.get("status")
        status_map = {
            1: "Scheduled",
            2: "Arrived",
            3: "Departed",
            4: "Delayed",
            5: "Cancelled"
        }
        if isinstance(raw_status, int):
            status = status_map.get(raw_status, "Unknown")
        else:
            status = raw_status or "Unknown"

        # Extract times
        est_dep = flight.get("departureTime")
        est_arr = flight.get("arrivalTime")

        return {
            "Status": status,
            "EstimatedDeparture": est_dep,
            "EstimatedArrival": est_arr
        }

    except Exception as e:
        return {"Status": "Error", "EstimatedDeparture": None, "EstimatedArrival": None}


# ============================================================
# HOME
# ============================================================
@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        company = request.form.get("company", "").lower().strip()

        if not company:
            flash("Please enter a company name.")
            return render_template("index.html")

        if not is_company_allowed(company):
            flash(f"Access denied for: {company}")
            return render_template("index.html")

        return redirect(url_for("flight_status", company=company))

    return render_template("index.html")


# ============================================================
# FLIGHT STATUS
# ============================================================
@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    global last_results

    company = request.args.get("company", "").lower().strip()
    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    flight_info = None
    uploaded_results = None

    # ---------- EXCEL UPLOAD ----------
    if "file" in request.files:
        file = request.files["file"]
        if file.filename:
            try:
                if file.filename.lower().endswith(".csv"):
                    df = pd.read_csv(file)
                else:
                    df = pd.read_excel(file)

                df.columns = df.columns.str.lower().str.strip()

                required = ["airline", "flightnumber", "departure", "arrival"]
                if not all(col in df.columns for col in required):
                    flash("File missing required columns.")
                    return redirect(url_for("flight_status", company=company))

                results = []
                for _, row in df.iterrows():
                    airline = str(row["airline"]).strip()
                    flightnum = str(row["flightnumber"]).strip()
                    dep = str(row["departure"]).strip()
                    arr = str(row["arrival"]).strip()

                    api_data = fetch_status_flightapi(airline, flightnum)

                    results.append({
                        "Airline": airline,
                        "FlightNumber": flightnum,
                        "From": dep,
                        "To": arr,
                        "Status": api_data["Status"],
                        "EstimatedDeparture": api_data["EstimatedDeparture"],
                        "EstimatedArrival": api_data["EstimatedArrival"]
                    })

                last_results = results
                return render_template("flight_status.html", company=company, uploaded_results=results)

            except Exception as e:
                flash(f"Error processing file: {e}")
                return redirect(url_for("flight_status", company=company))

    # ---------- MANUAL LOOKUP ----------
    if request.method == "POST":
        airline = request.form.get("airline", "").strip()
        flightnum = request.form.get("flight_number", "").strip()
        dep = request.form.get("departure", "").strip()
        arr = request.form.get("arrival", "").strip()

        api_data = fetch_status_flightapi(airline, flightnum)

        flight_info = {
            "Airline": airline,
            "FlightNumber": flightnum,
            "From": dep,
            "To": arr,
            "Status": api_data["Status"],
            "EstimatedDeparture": api_data["EstimatedDeparture"],
            "EstimatedArrival": api_data["EstimatedArrival"]
        }

        last_results = [flight_info]

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info
    )


# ============================================================
# DOWNLOAD STATUS RESULTS
# ============================================================
@app.route("/download-status")
def download_status():
    if not last_results:
        flash("No results to download.")
        return redirect(url_for("home"))

    output = BytesIO()
    pd.DataFrame(last_results).to_excel(output, index=False)
    output.seek(0)

    return send_file(output, as_attachment=True, download_name="flight_status.xlsx")


# ============================================================
# FLIGHT ANALYSIS (Amadeus)
# ============================================================
@app.route("/flight-analysis", methods=["GET", "POST"])
def flight_analysis():
    company = request.args.get("company", "").lower().strip()
    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    analysis_results = None
    return render_template("flight_analysis.html", company=company)

# ============================================================
# RUN APP
# ============================================================
if __name__ == "__main__":
    app.run(debug=True)



