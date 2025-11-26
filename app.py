import os
import json
from datetime import datetime, timedelta
from io import BytesIO

import pandas as pd
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "CHANGE_ME_IN_RENDER")

# ==========================================
#  ALLOWED COMPANIES
# ==========================================

ALLOWED_COMPANIES_FILE = "allowed_companies.json"


def load_allowed_companies():
    """Load allowed companies from JSON file, all lowercase."""
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        try:
            with open(ALLOWED_COMPANIES_FILE, "r") as f:
                companies = json.load(f)
            return [str(c).strip().lower() for c in companies]
        except Exception:
            return []
    return []


ALLOWED_COMPANIES = load_allowed_companies()
last_results = []            # flight status results
last_analysis_results = []   # flight analysis results (stubbed)


def is_company_allowed(company: str) -> bool:
    """
    If ALLOWED_COMPANIES is empty => allow all.
    Otherwise require company to be in list.
    """
    if not ALLOWED_COMPANIES:
        return True
    return company and company.lower() in ALLOWED_COMPANIES


# ==========================================
#  FLIGHTAPI.IO — FLIGHT STATUS
# ==========================================

FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY")  # set this in Render dashboard


def clean_date_for_api() -> str:
    """
    Returns "today" as YYYYMMDD for FlightAPI.io, roughly in US Central Time.
    Render runs in UTC, so we shift by -6 hours.
    """
    now_utc = datetime.utcnow()
    central_guess = now_utc - timedelta(hours=6)
    return central_guess.strftime("%Y%m%d")


def fetch_status_flightapi(airline: str, flight_number: str) -> dict:
    """
    Call FlightAPI.io for flight status.

    Returns a dict like:
    {
        "Status": "Scheduled / Delayed / In Air / Arrived / ...",
        "EstimatedDeparture": "HH:MM, Mon DD" or None,
        "EstimatedArrival": "HH:MM, Mon DD" or None
    }
    """
    if not FLIGHTAPI_KEY:
        return {
            "Status": "API Key Missing",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }

    airline_code = (airline or "").upper().strip()
    flight_no = str(flight_number).strip()
    date_str = clean_date_for_api()

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_no}&name={airline_code}&date={date_str}"
    )

    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            return {
                "Status": f"API Error {resp.status_code}",
                "EstimatedDeparture": None,
                "EstimatedArrival": None,
            }

        data = resp.json()

        # Case 1: list style (departure, arrival, status, etc.)
        if isinstance(data, list):
            status = None
            est_dep = None
            est_arr = None
            for block in data:
                if not isinstance(block, dict):
                    continue
                if "status" in block:
                    status = block.get("status")
                if "departure" in block:
                    est_dep = block["departure"].get("estimatedTime")
                if "arrival" in block:
                    est_arr = block["arrival"].get("estimatedTime")
            return {
                "Status": status or "Unknown",
                "EstimatedDeparture": est_dep,
                "EstimatedArrival": est_arr,
            }

        # Case 2: dict with "flights"
        if isinstance(data, dict) and "flights" in data:
            flights = data.get("flights") or []
            if not flights:
                return {
                    "Status": "Not Found",
                    "EstimatedDeparture": None,
                    "EstimatedArrival": None,
                }
            first = flights[0]
            status = first.get("displayStatus") or first.get("status") or "Unknown"
            est_dep = first.get("departureTime")
            est_arr = first.get("arrivalTime")
            return {
                "Status": status,
                "EstimatedDeparture": est_dep,
                "EstimatedArrival": est_arr,
            }

        return {
            "Status": "Not Found",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }

    except Exception as e:
        return {
            "Status": f"Error: {str(e)}",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }


# ==========================================
#  HOME PAGE
# ==========================================

@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        company = request.form.get("company", "").strip().lower()

        if not company:
            flash("Please enter a company name.")
            return render_template("index.html")

        if not is_company_allowed(company):
            flash(f"Access denied for company: {company}")
            return render_template("index.html")

        return redirect(url_for("flight_status", company=company))

    return render_template("index.html")


# ==========================================
#  FLIGHT STATUS PAGE
# ==========================================

@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    global last_results

    company = (request.args.get("company") or request.form.get("company") or "").strip().lower()

    if not is_company_allowed(company):
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

        status_data = fetch_status_flightapi(airline, flight_number)

        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": departure,
            "To": arrival,
            "Status": status_data.get("Status"),
            "EstimatedDeparture": status_data.get("EstimatedDeparture"),
            "EstimatedArrival": status_data.get("EstimatedArrival"),
        }

        last_results = [flight_info]

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results,
    )


# ==========================================
#  EXCEL UPLOAD (STATUS)
# ==========================================

@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results

    company = (request.args.get("company") or request.form.get("company") or "").strip().lower()
    if not is_company_allowed(company):
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
        # Read Excel or CSV
        if file.filename.lower().endswith(".csv"):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file, sheet_name=0)

        # Normalize columns
        df.columns = df.columns.str.strip().str.lower()

        required = ["airline", "flightnumber", "departure", "arrival"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            flash(f"Missing required columns: {missing}")
            return redirect(url_for("flight_status", company=company))

        results = []
        for _, row in df.iterrows():
            airline = str(row["airline"]).strip().upper()
            flight_number = str(row["flightnumber"]).strip()
            dep = str(row["departure"]).strip().upper()
            arr = str(row["arrival"]).strip().upper()

            status_data = fetch_status_flightapi(airline, flight_number)

            results.append(
                {
                    "Airline": airline,
                    "FlightNumber": flight_number,
                    "From": dep,
                    "To": arr,
                    "Status": status_data.get("Status"),
                    "EstimatedDeparture": status_data.get("EstimatedDeparture"),
                    "EstimatedArrival": status_data.get("EstimatedArrival"),
                }
            )

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


# ==========================================
#  DOWNLOAD STATUS RESULTS
# ==========================================

@app.route("/download")
def download_excel():
    if not last_results:
        flash("No results to download.")
        return redirect(url_for("home"))

    df = pd.DataFrame(last_results)
    out = BytesIO()
    df.to_excel(out, index=False)
    out.seek(0)

    return send_file(
        out,
        download_name="flight_status_results.xlsx",
        as_attachment=True,
    )


# ==========================================
#  FLIGHT ANALYSIS (STUB — AMADEUS DISABLED)
# ==========================================

@app.route("/flight-analysis", methods=["GET", "POST"])
def flight_analysis():
    """
    Flight Analysis page is kept, but Amadeus is DISABLED.

    The form still works, and we return stubbed rows with an Error
    message instead of calling the Amadeus API.
    """
    global last_analysis_results

    company = (request.args.get("company") or request.form.get("company") or "").strip().lower()
    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    analysis_results = None

    if request.method == "POST":
        origins = [o.strip().upper() for o in request.form.getlist("origins") if o.strip()]
        destinations = [d.strip().upper() for d in request.form.getlist("destinations") if d.strip()]
        outbound_date = request.form.get("outbound_date", "").strip()
        return_date = request.form.get("return_date", "").strip()

        if not origins or not destinations:
            flash("Please enter at least one origin and one destination.")
            return render_template("flight_analysis.html", company=company, analysis_results=None)

        if not outbound_date or not return_date:
            flash("Please select outbound and return dates.")
            return render_template("flight_analysis.html", company=company, analysis_results=None)

        # STUB: Do NOT call Amadeus, just return "API disabled"
        results = []
        for o in origins:
            for d in destinations:
                results.append(
                    {
                        "Origin": o,
                        "Destination": d,
                        "Price": None,
                        "Currency": "USD",
                        "Error": "Flight analysis API is currently disabled.",
                    }
                )

        last_analysis_results = results
        analysis_results = results

    return render_template(
        "flight_analysis.html",
        company=company,
        analysis_results=analysis_results,
    )


# ==========================================
#  DOWNLOAD ANALYSIS RESULTS
# ==========================================

@app.route("/download-analysis")
def download_analysis_excel():
    if not last_analysis_results:
        flash("No analysis results to download.")
        return redirect(url_for("home"))

    df = pd.DataFrame(last_analysis_results)
    out = BytesIO()
    df.to_excel(out, index=False)
    out.seek(0)

    return send_file(
        out,
        download_name="flight_analysis_results.xlsx",
        as_attachment=True,
    )


# ==========================================
#  HEALTH CHECK (for Render)
# ==========================================

@app.route("/health")
def health():
    return "OK", 200


# ==========================================
#  LOCAL RUN
# ==========================================

if __name__ == "__main__":
    app.run(debug=True)
