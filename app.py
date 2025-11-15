import os
import json
from datetime import datetime
from io import BytesIO

import pandas as pd
import requests
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_file,
)

app = Flask(__name__)
app.secret_key = "your-secret-key"  # change for production

# --------------------------------------
# Allowed Companies
# --------------------------------------
ALLOWED_COMPANIES_FILE = "allowed_companies.json"


def load_allowed_companies():
    """
    Load allowed companies from JSON file.
    If file missing or invalid, treat as no restriction.
    """
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        try:
            with open(ALLOWED_COMPANIES_FILE, "r") as f:
                data = json.load(f)
                return [str(c).strip().lower() for c in data]
        except Exception:
            return []
    return []


ALLOWED_COMPANIES = load_allowed_companies()
last_results = []  # used for Excel download


def is_company_allowed(company: str) -> bool:
    """
    If ALLOWED_COMPANIES is empty, allow all.
    Otherwise, require company to be in list.
    """
    if not ALLOWED_COMPANIES:
        return True
    if not company:
        return False
    return company.lower() in ALLOWED_COMPANIES


# --------------------------------------
# FlightAPI.io Settings
# --------------------------------------
# You can override this in Render env vars with FLIGHTAPI_KEY
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY", "69175603253bb1627f7ea9cc")

# Example:
# https://api.flightapi.io/airline/{API_KEY}?num=329&name=AA&date=20251114
FLIGHTAPI_URL = (
    "https://api.flightapi.io/airline/{key}?num={num}&name={airline}&date={date}"
)


# --------------------------------------
# Flight Status Fetcher
# --------------------------------------
def fetch_status(airline, flight_number, flight_date, departure=None, arrival=None):
    """
    Call FlightAPI.io and return a human-friendly status.

    Handles:
      - dict with "flights": [...]
      - simple list with {"status": "..."} as in DL 1636 example.
    """

    airline_code = airline.upper()  # VERY IMPORTANT: FlightAPI is case-sensitive
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

        status_map = {
            1: "Scheduled",
            2: "Arrived",
            3: "Departed",
            4: "Delayed",
            5: "Cancelled",
        }

        # Try to interpret flight_number as int if possible
        fn_int = None
        try:
            fn_int = int(flight_number)
        except Exception:
            pass

        # ------------------------------------------
        # Case 1: dict with "flights" list
        # ------------------------------------------
        if isinstance(data, dict) and "flights" in data:
            flights = data.get("flights") or []

            # First: exact route + flight number match
            exact_matches = []
            for f in flights:
                f_dep = str(f.get("departureAirportCode", "")).upper()
                f_arr = str(f.get("arrivalAirportCode", "")).upper()
                f_num = f.get("flightNumber")

                if dep == f_dep and arr == f_arr:
                    if fn_int is None or f_num == fn_int:
                        exact_matches.append(f)

            if exact_matches:
                # Prefer delayed/cancelled among exact matches
                severity_order = {5: 3, 4: 2, 3: 1, 2: 1, 1: 0}
                exact_sorted = sorted(
                    exact_matches,
                    key=lambda x: severity_order.get(x.get("status", 0)),
                    reverse=True,
                )
                best = exact_sorted[0]
                status = best.get("displayStatus") or status_map.get(best.get("status"))
                return status or "Unknown"

            # Second: match by departure airport (same flight number if possible)
            dep_matches = []
            for f in flights:
                f_dep = str(f.get("departureAirportCode", "")).upper()
                f_num = f.get("flightNumber")
                if f_dep == dep:
                    if fn_int is None or f_num == fn_int:
                        dep_matches.append(f)

            if dep_matches:
                # Prefer delayed/cancelled
                for f in dep_matches:
                    if f.get("status") in (4, 5):
                        return status_map.get(f.get("status"), "Unknown")
                # Otherwise return first
                f = dep_matches[0]
                return f.get("displayStatus") or status_map.get(f.get("status"), "Unknown")

            # Third: fallback to "most severe" across all flights (same flight number if possible)
            candidate_flights = []
            if fn_int is not None:
                candidate_flights = [f for f in flights if f.get("flightNumber") == fn_int]
            if not candidate_flights:
                candidate_flights = flights

            if candidate_flights:
                severity_order = {5: 3, 4: 2, 3: 1, 2: 1, 1: 0}
                sorted_flights = sorted(
                    candidate_flights,
                    key=lambda x: severity_order.get(x.get("status", 0)),
                    reverse=True,
                )
                best = sorted_flights[0]
                status = best.get("displayStatus") or status_map.get(best.get("status"))
                return status or "Unknown"

            return "Not Found"

        # ------------------------------------------
        # Case 2: list-style response
        # e.g. [ {departure:..}, {arrival:..}, {status:"In Air"} ]
        # ------------------------------------------
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "status" in item:
                    return item.get("status") or "Unknown"
            return "Not Found"

        return "Not Found"

    except Exception:
        return "Not Found"


# --------------------------------------
# HOME PAGE (company access)
# --------------------------------------
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


# --------------------------------------
# FLIGHT STATUS PAGE
# --------------------------------------
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


# --------------------------------------
# EXCEL UPLOAD
# --------------------------------------
@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results

    # keep company (from query or form)
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
        # Read Excel/CSV
        if file.filename.lower().endswith(".csv"):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file, sheet_name=0)

        # Normalize headers to lowercase
        df.columns = df.columns.str.strip().str.lower()

        required = ["airline", "flightnumber", "departure", "arrival"]
        missing = [c for c in required if c not in df.columns]

        if missing:
            flash(f"Missing required columns: {missing}")
            return redirect(url_for("flight_status", company=company))

        today = datetime.today().strftime("%Y-%m-%d")
        results = []

        for _, row in df.iterrows():
            airline = str(row["airline"]).strip().upper()
            flight_number = str(row["flightnumber"]).strip()
            departure = str(row["departure"]).strip().upper()
            arrival = str(row["arrival"]).strip().upper()

            status = fetch_status(airline, flight_number, today, departure, arrival)

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
            company=company,
            flight_info=None,
            uploaded_results=results,
        )

    except Exception as e:
        flash(f"Error processing file: {e}")
        return redirect(url_for("flight_status", company=company))


# --------------------------------------
# DOWNLOAD EXCEL
# --------------------------------------
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


# --------------------------------------
# MAIN
# --------------------------------------
if __name__ == "__main__":
    app.run(debug=True)
