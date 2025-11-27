import os
import json
import time
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

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

# ------------------------------------------------------------------------------
# Flask app + secret
# ------------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key")

# ------------------------------------------------------------------------------
# Allowed companies handling
# ------------------------------------------------------------------------------

ALLOWED_COMPANIES_FILE = "allowed_companies.json"


def load_allowed_companies():
    if os.path.exists(ALLOWED_COMPANIES_FILE):
        try:
            with open(ALLOWED_COMPANIES_FILE, "r") as f:
                companies = json.load(f)
                return [str(c).strip().lower() for c in companies]
        except Exception:
            return []
    return []


ALLOWED_COMPANIES = load_allowed_companies()

# For downloads
last_results = []            # flight status results
last_analysis_results = []   # flight analysis results


def is_company_allowed(company: str) -> bool:
    if not ALLOWED_COMPANIES:
        # If no config file or empty list, allow everyone
        return True
    if not company:
        return False
    return company.lower() in ALLOWED_COMPANIES


# ------------------------------------------------------------------------------
# Helpers: Date in America/Chicago (for FlightAPI date param)
# ------------------------------------------------------------------------------

def today_central_yyyymmdd() -> str:
    """Return today's date in America/Chicago as YYYYMMDD."""
    try:
        now_central = datetime.now(ZoneInfo("America/Chicago"))
    except Exception:
        now_central = datetime.utcnow()
    return now_central.strftime("%Y%m%d")


# ------------------------------------------------------------------------------
# FlightAPI.io (flight status)
# ------------------------------------------------------------------------------

FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY")


def fetch_status_flightapi(
    airline: str,
    flight_number: str,
    flight_date_yyyymmdd: str,
    departure: str | None = None,
    arrival: str | None = None,
) -> dict:
    """
    Call FlightAPI.io /airline endpoint and return a normalized dict:

    {
        "Status": "Arrived" / "Scheduled" / "Delayed" / ...,
        "EstimatedDeparture": "09:29, Nov 26" or None,
        "EstimatedArrival": "10:02, Nov 26" or None
    }

    - Matches airline + flight number.
    - Prefers flights whose departure/arrival airport codes match the
      provided departure/arrival (exact direction).
    - If no match found by direction, falls back to the first result.
    """
    if not FLIGHTAPI_KEY:
        return {
            "Status": "Missing FLIGHTAPI_KEY",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }

    airline_code = airline.strip().lower()
    flight_number = str(flight_number).strip()
    date_param = flight_date_yyyymmdd.strip()

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline_code}&date={date_param}"
    )

    try:
        resp = requests.get(url, timeout=20)
    except Exception as e:
        return {
            "Status": f"Request error: {e}",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }

    if resp.status_code != 200:
        return {
            "Status": f"API Error ({resp.status_code})",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }

    try:
        data = resp.json()
    except Exception as e:
        return {
            "Status": f"JSON error: {e}",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }

    # ------------------------------------------------------------------
    # Shape 1: { "flights": [ {...}, ... ], "flight": null, ... }
    # ------------------------------------------------------------------
    if isinstance(data, dict) and "flights" in data:
        flights = data.get("flights") or []
        if not flights:
            return {
                "Status": "Not Found",
                "EstimatedDeparture": None,
                "EstimatedArrival": None,
            }

        dep_code = departure.strip().upper() if departure else None
        arr_code = arrival.strip().upper() if arrival else None

        def matches_direction(f: dict) -> bool:
            if dep_code and f.get("departureAirportCode", "").upper() != dep_code:
                return False
            if arr_code and f.get("arrivalAirportCode", "").upper() != arr_code:
                return False
            return True

        matching = [f for f in flights if matches_direction(f)]
        if not matching:
            # Fallback: no direction-specific match, use all flights
            matching = flights

        # Sort by departureTime if parseable, earliest first
        def parse_departure_time(f: dict):
            t = f.get("departureTime")
            if not t:
                return datetime.max
            # Example format: "09:29, Nov 26" – we add the given year
            try:
                dt_no_year = datetime.strptime(t, "%H:%M, %b %d")
                year = int(flight_date_yyyymmdd[:4])
                return dt_no_year.replace(year=year)
            except Exception:
                return datetime.max

        chosen = sorted(matching, key=parse_departure_time)[0]

        # Map status
        status_val = chosen.get("displayStatus") or chosen.get("status")
        if isinstance(status_val, int):
            status_map = {
                1: "Scheduled",
                2: "Arrived",
                3: "Departed",
                4: "Delayed",
                5: "Cancelled",
            }
            status_text = status_map.get(status_val, "Unknown")
        else:
            status_text = status_val or "Unknown"

        est_dep = chosen.get("departureTime")
        est_arr = chosen.get("arrivalTime")

        return {
            "Status": status_text,
            "EstimatedDeparture": est_dep,
            "EstimatedArrival": est_arr,
        }

    # ------------------------------------------------------------------
    # Shape 2: [ {"departure": {...}}, {"arrival": {...}}, {"status": "..."} ]
    # ------------------------------------------------------------------
    if isinstance(data, list):
        departure_block = {}
        arrival_block = {}
        status_text = "Unknown"

        for block in data:
            if isinstance(block, dict):
                if "departure" in block:
                    departure_block = block["departure"] or {}
                elif "arrival" in block:
                    arrival_block = block["arrival"] or {}
                elif "status" in block:
                    status_text = block.get("status") or "Unknown"

        est_dep = (
            departure_block.get("estimatedTime")
            or departure_block.get("scheduledTime")
        )
        est_arr = (
            arrival_block.get("estimatedTime")
            or arrival_block.get("scheduledTime")
        )

        return {
            "Status": status_text,
            "EstimatedDeparture": est_dep,
            "EstimatedArrival": est_arr,
        }

    # Unknown shape
    return {
        "Status": "Not Found",
        "EstimatedDeparture": None,
        "EstimatedArrival": None,
    }


# ------------------------------------------------------------------------------
# Amadeus (Flight Analysis)
# ------------------------------------------------------------------------------

AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")

AMADEUS_AUTH_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
AMADEUS_FLIGHT_OFFERS_URL = "https://test.api.amadeus.com/v2/shopping/flight-offers"

amadeus_token = None
amadeus_token_expiry = 0  # epoch seconds


def get_amadeus_token():
    """Get or refresh Amadeus OAuth token."""
    global amadeus_token, amadeus_token_expiry

    if not AMADEUS_CLIENT_ID or not AMADEUS_CLIENT_SECRET:
        raise RuntimeError("Amadeus credentials not configured")

    now = time.time()
    if amadeus_token and now < amadeus_token_expiry:
        return amadeus_token

    data = {
        "grant_type": "client_credentials",
        "client_id": AMADEUS_CLIENT_ID,
        "client_secret": AMADEUS_CLIENT_SECRET,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    resp = requests.post(AMADEUS_AUTH_URL, data=data, headers=headers, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"Amadeus auth failed: {resp.status_code} {resp.text}")

    payload = resp.json()
    amadeus_token = payload.get("access_token")
    expires_in = payload.get("expires_in", 1800)
    amadeus_token_expiry = now + expires_in - 60  # refresh slightly early

    return amadeus_token


def search_lowest_fare_amadeus(origin, destination, departure_date, return_date):
    """
    Search lowest roundtrip fare using Amadeus.

    origin/destination: IATA codes
    dates: YYYY-MM-DD
    """
    try:
        token = get_amadeus_token()
    except Exception as e:
        return {
            "Origin": origin,
            "Destination": destination,
            "Error": f"Auth error: {e}",
        }

    params = {
        "originLocationCode": origin,
        "destinationLocationCode": destination,
        "departureDate": departure_date,
        "returnDate": return_date,
        "adults": 1,
        "currencyCode": "USD",
        "max": 1,
    }
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = requests.get(
            AMADEUS_FLIGHT_OFFERS_URL,
            headers=headers,
            params=params,
            timeout=30,
        )
        if resp.status_code != 200:
            return {
                "Origin": origin,
                "Destination": destination,
                "Error": f"API error {resp.status_code}",
            }

        data = resp.json()
        offers = data.get("data", [])
        if not offers:
            return {
                "Origin": origin,
                "Destination": destination,
                "Error": "No fares found",
            }

        offer = offers[0]
        price_info = offer.get("price", {})
        total = price_info.get("grandTotal")
        currency = price_info.get("currency", "USD")

        return {
            "Origin": origin,
            "Destination": destination,
            "DepartureDate": departure_date,
            "ReturnDate": return_date,
            "Price": total,
            "Currency": currency,
            "Error": "",
        }

    except Exception as e:
        return {
            "Origin": origin,
            "Destination": destination,
            "Error": f"Request error: {e}",
        }


# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------

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


# --------------------------- Flight Status ------------------------------------

@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    global last_results

    company = (
        request.args.get("company")
        or request.form.get("company")
        or ""
    ).strip().lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    flight_info = None
    uploaded_results = None

    # Distinguish between manual lookup and file upload
    if request.method == "POST":
        # FILE UPLOAD POST comes to /upload/<company>, not here
        # Manual form posts directly here.
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

        flight_date = today_central_yyyymmdd()
        status_data = fetch_status_flightapi(
            airline=airline,
            flight_number=flight_number,
            flight_date_yyyymmdd=flight_date,
            departure=departure,
            arrival=arrival,
        )

        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": departure,
            "To": arrival,
            "Status": status_data.get("Status", "Unknown"),
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


# Separate upload endpoint so URL stays /upload/<company> as your logs show
@app.route("/upload/<company>", methods=["POST"])
def upload_file(company):
    global last_results

    company = (company or "").strip().lower()
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
        if file.filename.lower().endswith(".csv"):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file, sheet_name=0)

        df.columns = df.columns.str.strip().str.lower()
        required_cols = ["airline", "flightnumber", "departure", "arrival"]
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            flash(f"Missing required columns: {missing}")
            return redirect(url_for("flight_status", company=company))

        results = []
        flight_date = today_central_yyyymmdd()

        for _, row in df.iterrows():
            airline = str(row["airline"]).strip().upper()
            flight_number = str(row["flightnumber"]).strip()
            dep = str(row["departure"]).strip().upper()
            arr = str(row["arrival"]).strip().upper()

            status_data = fetch_status_flightapi(
                airline=airline,
                flight_number=flight_number,
                flight_date_yyyymmdd=flight_date,
                departure=dep,
                arrival=arr,
            )

            results.append(
                {
                    "Airline": airline,
                    "FlightNumber": flight_number,
                    "From": dep,
                    "To": arr,
                    "Status": status_data.get("Status", "Unknown"),
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


# --------------------------- Flight Analysis (Amadeus) ------------------------

@app.route("/flight-analysis", methods=["GET", "POST"])
def flight_analysis():
    global last_analysis_results

    company = (
        request.args.get("company")
        or request.form.get("company")
        or ""
    ).strip().lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    analysis_results = None

    if request.method == "POST":
        origins = [
            o.strip().upper()
            for o in request.form.getlist("origins")
            if o.strip()
        ]
        destinations = [
            d.strip().upper()
            for d in request.form.getlist("destinations")
            if d.strip()
        ]

        outbound_date = request.form.get("outbound_date", "").strip()
        return_date = request.form.get("return_date", "").strip()

        if not origins or not destinations:
            flash("Please enter at least one origin and one destination.")
            return render_template(
                "flight_analysis.html",
                company=company,
                analysis_results=None,
            )

        if not outbound_date or not return_date:
            flash("Please select outbound and return dates.")
            return render_template(
                "flight_analysis.html",
                company=company,
                analysis_results=None,
            )

        analysis_results = []
        for origin in origins:
            for dest in destinations:
                result = search_lowest_fare_amadeus(
                    origin,
                    dest,
                    outbound_date,
                    return_date,
                )
                analysis_results.append(result)

        last_analysis_results = analysis_results

    return render_template(
        "flight_analysis.html",
        company=company,
        analysis_results=analysis_results,
    )


@app.route("/download-analysis")
def download_analysis_excel():
    if not last_analysis_results:
        flash("No analysis results to download.")
        return redirect(url_for("home"))

    df = pd.DataFrame(last_analysis_results)
    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(
        output,
        download_name="flight_analysis_results.xlsx",
        as_attachment=True,
    )


# ------------------------------------------------------------------------------
# Entry point (for local dev; Render uses gunicorn)
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
