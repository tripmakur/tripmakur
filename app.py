import os
import json
import requests
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from datetime import datetime
from io import BytesIO

app = Flask(__name__)
app.secret_key = "your-secret-key"

# -----------------------
# Load Allowed Companies
# -----------------------
ALLOWED_COMPANIES_FILE = "allowed_companies.json"

def load_allowed_companies():
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        with open(ALLOWED_COMPANIES_FILE, "r") as f:
            return [c.strip().lower() for c in json.load(f)]
    return []

ALLOWED_COMPANIES = load_allowed_companies()

# store results for download
last_results = []

# -----------------------
# FlightAPI.io SETTINGS
# -----------------------
FLIGHTAPI_KEY = "69175603253bb1627f7ea9cc"
FLIGHTAPI_URL = "https://api.flightapi.io/airline/{key}?num={num}&name={airline}&date={date}"


# -----------------------
# Fetch Status Function
# -----------------------
def fetch_status(airline, flight_number, flight_date):
    """
    Query FlightAPI.io and return ONLY the displayStatus.
    Returns 'Not Found' if no usable data exists.
    """

    airline = airline.lower()
    url = FLIGHTAPI_URL.format(
        key=FLIGHTAPI_KEY,
        num=flight_number,
        name=airline,
        date=flight_date.replace("-", "")
    )

    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return "Not Found"

        data = response.json()

        # if API returns direct list (AA example)
        if isinstance(data, list):
            # last element usually contains status
            for item in data:
                if isinstance(item, dict) and "status" in item:
                    return item["status"]
            return "Not Found"

        # standard format with flights key
        flights = data.get("flights", [])

        if flights and isinstance(flights, list):
            # take FIRST flight always
            first = flights[0]
            status = first.get("displayStatus") or first.get("status")
            return status if status else "Not Found"

        return "Not Found"

    except Exception as e:
        return "Not Found"


# -----------------------
# Home Page
# -----------------------
@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        company = request.form.get("company", "").strip().lower()

        if not company:
            flash("Please enter your company name.")
            return render_template("index.html")

        if company not in ALLOWED_COMPANIES:
            flash("Access denied: unauthorized company.")
            return render_template("index.html")

        return redirect(url_for("flight_status", company=company))

    return render_template("index.html")


# -----------------------
# Flight Status Page
# -----------------------
@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    global last_results

    company = (
        request.args.get("company")
        or request.form.get("company")
        or ""
    ).strip().lower()

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
            return render_template(
                "flight_status.html",
                company=company,
                flight_info=None,
                uploaded_results=None
            )

        today = datetime.today().strftime("%Y-%m-%d")

        status = fetch_status(airline, flight_number, today)

        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": departure,
            "To": arrival,
            "Status": status
        }

        last_results = [flight_info]

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results
    )


# -----------------------
# Excel Upload Handler
# -----------------------
@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results

    # Keep company authorization
    company = request.form.get("company", "").strip().lower()
    if company and company not in ALLOWED_COMPANIES:
        flash("Access denied.")
        return redirect(url_for("home"))

    # Validate file upload
    if "file" not in request.files:
        flash("No file uploaded.")
        return redirect(url_for("flight_status", company=company))

    file = request.files["file"]
    if not file.filename:
        flash("No selected file.")
        return redirect(url_for("flight_status", company=company))

    try:
        # Read the file
        df = pd.read_csv(file) if file.filename.endswith(".csv") else pd.read_excel(file)

        # Normalize headers: remove spaces, tabs, weird characters
        df.columns = (
            df.columns
            .str.strip()             # remove leading/trailing spaces
            .str.replace(r'\s+', '', regex=True)  # remove all spaces
            .str.replace(r'[^a-zA-Z]', '', regex=True)  # remove non-letters
            .str.lower()
        )

        required = ["airline", "flightnumber", "departure", "arrival"]
        missing = [c for c in required if c not in df.columns]

        if missing:
            flash(f"Missing required columns: {missing}")
            return redirect(url_for("flight_status", company=company))

        today = datetime.today().strftime("%Y-%m-%d")
        results = []

        for _, row in df.iterrows():
            # safe extraction
            airline = str(row.get("airline", "")).strip().upper()
            flight_number = str(row.get("flightnumber", "")).strip()
            departure = str(row.get("departure", "")).strip().upper()
            arrival = str(row.get("arrival", "")).strip().upper()

            # skip rows missing data
            if airline == "" or flight_number == "":
                results.append({
                    "Airline": airline,
                    "FlightNumber": flight_number,
                    "From": departure,
                    "To": arrival,
                    "Status": "Missing Data",
                })
                continue

            status = fetch_status(airline, flight_number, today)

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




# -----------------------
# Excel Download
# -----------------------
@app.route("/download")
def download_excel():
    if not last_results:
        flash("No results to download.")
        return redirect(url_for("home"))

    output = BytesIO()
    pd.DataFrame(last_results).to_excel(output, index=False)
    output.seek(0)

    return send_file(
        output,
        download_name="flight_status_results.xlsx",
        as_attachment=True
    )


if __name__ == "__main__":
    app.run(debug=True)
