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

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "your-secret-key")

# =========================================
# Allowed Companies
# =========================================
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

last_results: list[dict] = []          # For flight status download
last_analysis_results: list[dict] = [] # For flight analysis download


def is_company_allowed(company: str) -> bool:
    """
    If ALLOWED_COMPANIES is empty, allow all.
    Otherwise, company must appear in the list (case-insensitive).
    """
    if not ALLOWED_COMPANIES:
        return True
    if not company:
        return False
    return company.lower() in ALLOWED_COMPANIES


# =========================================
# Helpers: "Today" in US Central (CST/CDT)
# =========================================
def get_central_today_str() -> str:
    """
    Returns today's date in America/Chicago as YYYY-MM-DD.
    This is what we send to FlightAPI for status lookups.
    """
    now_central = datetime.now(ZoneInfo("America/Chicago"))
    return now_central.strftime("%Y-%m-%d")


def format_time_label(raw: str | None) -> str | None:
    """
    We keep FlightAPI's formatted times (e.g. '20:30, Nov 26') as-is.
    This helper is here in case you ever want to adjust formatting.
    """
    if not raw:
        return None
    return raw


# =========================================
# FlightAPI.io Settings (Status)
# =========================================
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY")


def fetch_status_flightapi(airline: str, flight_number: str, flight_date: str) -> dict:
    """
    Fetch a flight's status (and estimated times) from FlightAPI.io.

    Returns a dict:
    {
        "Status": "Scheduled" | "Arrived" | "Delayed" | ... | "Not Found" | "Error: ...",
        "EstimatedDeparture": "HH:MM, Mon DD" or None,
        "EstimatedArrival": "HH:MM, Mon DD" or None
    }
    """
    if not FLIGHTAPI_KEY:
        return {
            "Status": "Config Error (FLIGHTAPI_KEY not set)",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }

    airline_code = airline.upper()
    date_str = flight_date.replace("-", "")  # FlightAPI expects YYYYMMDD

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline_code}&date={date_str}"
    )

    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            return {
                "Status": f"API Error ({resp.status_code})",
                "EstimatedDeparture": None,
                "EstimatedArrival": None,
            }

        data = resp.json()

        # ---------------------------
        # Case 1: list-style response
        # [ { "departure": {...} }, { "arrival": {...} }, { "status": "..." }, ... ]
        # ---------------------------
        if isinstance(data, list):
            departure_block = next(
                (b.get("departure") for b in data if isinstance(b, dict) and "departure" in b),
                {},
            )
            arrival_block = next(
                (b.get("arrival") for b in data if isinstance(b, dict) and "arrival" in b),
                {},
            )
            status_block = next(
                (b for b in data if isinstance(b, dict) and "status" in b),
                {},
            )

            status_text = status_block.get("status") or "Unknown"

            est_dep = departure_block.get("estimatedTime") or departure_block.get("scheduledTime")
            est_arr = arrival_block.get("estimatedTime") or arrival_block.get("scheduledTime")

            return {
                "Status": status_text,
                "EstimatedDeparture": format_time_label(est_dep),
                "EstimatedArrival": format_time_label(est_arr),
            }

        # ---------------------------
        # Case 2: dict with "flights": [...]
        # {
        #   "flights": [
        #     {
        #       "displayStatus": "Arrived",
        #       "departureTime": "15:33, Nov 14",
        #       "arrivalTime": "16:01, Nov 14",
        #       ...
        #     },
        #   ],
        #   ...
        # }
        # ---------------------------
        if isinstance(data, dict) and "flights" in data:
            flights = data.get("flights") or []
            if not flights:
                return {
                    "Status": "Not Found",
                    "EstimatedDeparture": None,
                    "EstimatedArrival": None,
                }

            first = flights[0]

            status_val = first.get("displayStatus") or first.get("status")
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

            est_dep = first.get("departureTime")
            est_arr = first.get("arrivalTime")

            return {
                "Status": status_text,
                "EstimatedDeparture": format_time_label(est_dep),
                "EstimatedArrival": format_time_label(est_arr),
            }

        # Unknown shape
        return {
            "Status": "Not Found",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }

    except Exception as e:
        return {
            "Status": f"Error: {e}",
            "EstimatedDeparture": None,
            "EstimatedArrival": None,
        }


# =========================================
# Amadeus Settings (Flight Analysis)
# =========================================
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")

AMADEUS_AUTH_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
AMADEUS_FLIGHT_OFFERS_URL = "https://test.api.amadeus.com/v2/shopping/flight-offers"

amadeus_token: str | None = None
amadeus_token_expiry: float = 0.0  # epoch seconds


def get_amadeus_token() -> str:
    """
    Get or refresh Amadeus OAuth token.
    Raises RuntimeError on failure; caller should catch and convert to an Error field.
    """
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

    if not amadeus_token:
        raise RuntimeError("Amadeus returned no access_token")

    return amadeus_token


def search_lowest_fare_amadeus(origin: str, destination: str,
                               departure_date: str, return_date: str) -> dict:
    """
    Option A: Return ONLY the lowest price & currency (plus an Error field).

    Returns dict:
    {
      "Origin": origin,
      "Destination": destination,
      "Price": "123.45" or None,
      "Currency": "USD",
      "Error": "" or message
    }
    """
    try:
        token = get_amadeus_token()
    except Exception as e:
        return {
            "Origin": origin,
            "Destination": destination,
            "Price": None,
            "Currency": "USD",
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
                "Price": None,
                "Currency": "USD",
                "Error": f"API error {resp.status_code}",
            }

        data = resp.json()
        offers = data.get("data", [])
        if not offers:
            return {
                "Origin": origin,
                "Destination": destination,
                "Price": None,
                "Currency": "USD",
                "Error": "No fares found",
            }

        offer = offers[0]
        price_info = offer.get("price", {})
        total = price_info.get("grandTotal")
        currency = price_info.get("currency", "USD")

        return {
            "Origin": origin,
            "Destination": destination,
            "Price": total,
            "Currency": currency,
            "Error": "",
        }

    except Exception as e:
        return {
            "Origin": origin,
            "Destination": destination,
            "Price": None,
            "Currency": "USD",
            "Error": f"Request error: {e}",
        }


# =========================================
# HOME PAGE  (Company gate)
# =========================================
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


# =========================================
# FLIGHT STATUS PAGE
# =========================================
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
        airline = request.form.get("airline", "").strip()
        flight_number = request.form.get("flight_number", "").strip()
        departure = request.form.get("departure", "").strip()
        arrival = request.form.get("arrival", "").strip()

        if not all([airline, flight_number, departure, arrival]):
            flash("All fields are required.")
            return render_template(
                "flight_status.html",
                company=company,
                flight_info=None,
                uploaded_results=None,
            )

        today = get_central_today_str()

        # Normalize airline to upper for the API, but keep original if you want
        status_info = fetch_status_flightapi(airline.upper(), flight_number, today)

        flight_info = {
            "Airline": airline.upper(),
            "FlightNumber": flight_number,
            "From": departure.upper(),
            "To": arrival.upper(),
            "Status": status_info.get("Status"),
            "EstimatedDeparture": status_info.get("EstimatedDeparture"),
            "EstimatedArrival": status_info.get("EstimatedArrival"),
        }

        last_results = [flight_info]

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results,
    )


# =========================================
# EXCEL UPLOAD (Status)
# =========================================
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
        # Read file (Excel or CSV)
        if file.filename.lower().endswith(".csv"):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file, sheet_name=0)

        # Normalize column names
        df.columns = df.columns.str.strip().str.lower()

        required = ["airline", "flightnumber", "departure", "arrival"]
        missing = [c for c in required if c not in df.columns]

        if missing:
            flash(f"Missing required columns: {missing}")
            return redirect(url_for("flight_status", company=company))

        today = get_central_today_str()
        results = []

        for _, row in df.iterrows():
            airline = str(row["airline"]).strip()
            flight_number = str(row["flightnumber"]).strip()
            dep = str(row["departure"]).strip()
            arr = str(row["arrival"]).strip()

            status_info = fetch_status_flightapi(airline.upper(), flight_number, today)

            results.append(
                {
                    "Airline": airline.upper(),
                    "FlightNumber": flight_number,
                    "From": dep.upper(),
                    "To": arr.upper(),
                    "Status": status_info.get("Status"),
                    "EstimatedDeparture": status_info.get("EstimatedDeparture"),
                    "EstimatedArrival": status_info.get("EstimatedArrival"),
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


# =========================================
# DOWNLOAD STATUS RESULTS
# =========================================
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


# =========================================
# FLIGHT ANALYSIS PAGE (Amadeus, Option A)
# =========================================
@app.route("/flight-analysis", methods=["GET", "POST"])
def flight_analysis():
    global last_analysis_results

    company = (request.args.get("company") or request.form.get("company") or "").strip().lower()

    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    analysis_results = None

    if request.method == "POST":
        # Origins / Destinations come from repeated fields named
        # "origins" and "destinations" in flight_analysis.html
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

                row = {
                    "Origin": origin,
                    "Destination": dest,
                    "Price": result.get("Price"),
                    "Currency": result.get("Currency", "USD"),
                    "Error": result.get("Error", ""),
                }
                analysis_results.append(row)

        last_analysis_results = analysis_results

    return render_template(
        "flight_analysis.html",
        company=company,
        analysis_results=analysis_results,
    )


# =========================================
# DOWNLOAD ANALYSIS RESULTS
# =========================================
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


# =========================================
# MAIN (for local testing)
# =========================================
if __name__ == "__main__":
    # For local dev only; Render will use gunicorn
    app.run(debug=True, host="0.0.0.0", port=5000)




