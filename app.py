import os
import json
from flask import Flask, render_template, request, redirect, url_for, flash, send_file
import pandas as pd
from datetime import datetime
from io import BytesIO

app = Flask(__name__)
app.secret_key = "your-secret-key"

# -------------------------
# Load Allowed Companies
# -------------------------
ALLOWED_COMPANIES_FILE = "allowed_companies.json"


def load_allowed_companies():
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        with open(ALLOWED_COMPANIES_FILE, "r") as f:
            companies = json.load(f)
            return [c.strip().lower() for c in companies]
    return []


ALLOWED_COMPANIES = load_allowed_companies()
last_results = []


# -------------------------
# Dummy Flight Status API
# -------------------------
def fetch_status(airline, flight_number, flight_date):
    return "On Time"  # placeholder


# -------------------------
# Home Page
# -------------------------
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

        return redirect(url_for("flight_status", company=company))

    return render_template("index.html")


# -------------------------
# Flight Status Page
# -------------------------
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
        flight_date = request.form.get("flight_date", "").strip()

        if not all([airline, flight_number, departure, arrival]):
            flash("All fields are required.")
            return render_template(
                "flight_status.html",
                company=company,
                flight_info=None,
                uploaded_results=None,
            )

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
        uploaded_results=uploaded_results,
    )


# -------------------------
# Excel Upload
# -------------------------
@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results

    if "file" not in request.files:
        flash("No file uploaded")
        return redirect(url_for("flight_status"))

    file = request.files["file"]

    if file.filename == "":
        flash("No selected file")
        return redirect(url_for("flight_status"))

    try:
        # Read Excel or CSV
        filename = file.filename.lower()

        if filename.endswith(".xlsx"):
            df = pd.read_excel(file)
        elif filename.endswith(".csv"):
            df = pd.read_csv(file)
        else:
            flash("Unsupported file format.")
            return redirect(url_for("flight_status"))

        # Normalize column names (strip, lowercase)
        df.columns = df.columns.str.strip().str.lower()

        # Expected normalized names
        required = {"airline", "flightnumber", "from", "to"}

        # Map alternate names
        column_map = {
            "departure": "from",
            "arrival": "to",
        }

        # Apply mapping
        df.rename(columns=column_map, inplace=True)

        # After renaming, check for required columns
        if not required.issubset(set(df.columns)):
            flash(
                f"Missing required columns. Found: {list(df.columns)} "
                f"Required: Airline, FlightNumber, From, To"
            )
            return redirect(url_for("flight_status"))

        # Process
        results = []

        for _, row in df.iterrows():
            airline = str(row["airline"]).strip().upper()
            flight_number = str(row["flightnumber"]).strip()
            departure = str(row["from"]).strip().upper()
            arrival = str(row["to"]).strip().upper()

            status = fetch_status(airline, flight_number)

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
            flight_info=None,
            uploaded_results=results,
        )

    except Exception as e:
        flash(f"Error processing file: {str(e)}")
        return redirect(url_for("flight_status"))



# -------------------------
# Download Excel
# -------------------------
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
















