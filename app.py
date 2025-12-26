import os
import json
import time
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from io import BytesIO

import pandas as pd
import requests
import pytz
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_file,
    jsonify,
    session,
)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "your-secret-key")

# ==============================
# Allowed Companies
# ==============================
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

# Last results for Excel download
last_status_results = []
last_analysis_results = []


def is_company_allowed(company: str) -> bool:
    if not ALLOWED_COMPANIES:
        # If list is empty, allow all
        return True
    if not company:
        return False
    return company.lower() in ALLOWED_COMPANIES


# ==============================
# FlightAPI.io (Status)
# ==============================
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY", "69175603253bb1627f7ea9cc")


def map_status_code(code):
    mapping = {
        1: "Scheduled",
        2: "Arrived",
        3: "Departed",
        4: "Delayed",
        5: "Cancelled",
    }
    return mapping.get(code, "Unknown")


def fetch_status_flightapi(
    airline: str,
    flight_number: str,
    departure_airport: str = None,
    arrival_airport: str = None,
) -> dict:
    """
    Call FlightAPI.io /airline endpoint and return a dict:
    {
        "status": "Arrived / Delayed / ...",
        "estimated_departure": "..." or None,
        "estimated_arrival": "..." or None
    }
    """
    airline_name_param = airline.lower().strip()
    flight_number = str(flight_number).strip()

    # Use today's date in America/Chicago
    central = pytz.timezone("America/Chicago")
    today_central = datetime.now(central)
    date_param = today_central.strftime("%Y%m%d")

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline_name_param}&date={date_param}"
    )

    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            return {
                "status": f"API error {resp.status_code}",
                "estimated_departure": None,
                "estimated_arrival": None,
            }

        data = resp.json()

        # Case A: dict with "flights"
        if isinstance(data, dict) and "flights" in data:
            flights = data.get("flights") or []
            if not flights:
                return {
                    "status": "Not Found",
                    "estimated_departure": None,
                    "estimated_arrival": None,
                }

            dep_code = (departure_airport or "").upper().strip()
            arr_code = (arrival_airport or "").upper().strip()

            chosen = None
            # Try to match by route if possible
            if dep_code and arr_code:
                for f in flights:
                    if (
                        f.get("departureAirportCode", "").upper() == dep_code
                        and f.get("arrivalAirportCode", "").upper() == arr_code
                    ):
                        chosen = f
                        break

            if chosen is None:
                chosen = flights[0]

            status_text = chosen.get("displayStatus")
            if not status_text:
                code = chosen.get("status")
                if isinstance(code, int):
                    status_text = map_status_code(code)
                else:
                    status_text = "Unknown"

            dep_time = chosen.get("departureTime")
            arr_time = chosen.get("arrivalTime")

            return {
                "status": status_text,
                "estimated_departure": dep_time,
                "estimated_arrival": arr_time,
            }

        # Case B: legacy list format (departure / arrival / status)
        if isinstance(data, list):
            dep_block = {}
            arr_block = {}
            status_block = {}

            for block in data:
                if not isinstance(block, dict):
                    continue
                if "departure" in block:
                    dep_block = block.get("departure") or {}
                elif "arrival" in block:
                    arr_block = block.get("arrival") or {}
                elif "status" in block:
                    status_block = block

            status_text = status_block.get("status") or "Unknown"

            dep_time = dep_block.get("estimatedTime") or dep_block.get("scheduledTime")
            arr_time = arr_block.get("estimatedTime") or arr_block.get("scheduledTime")

            return {
                "status": status_text,
                "estimated_departure": dep_time,
                "estimated_arrival": arr_time,
            }

        return {
            "status": "Unknown",
            "estimated_departure": None,
            "estimated_arrival": None,
        }

    except Exception as e:
        return {
            "status": f"Error: {e}",
            "estimated_departure": None,
            "estimated_arrival": None,
        }


# ==============================
# Amadeus (Flight Analysis)
# ==============================
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
        raise RuntimeError("Amadeus credentials not configured in environment")

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
    amadeus_token_expiry = now + expires_in - 60

    return amadeus_token


def search_lowest_fare_amadeus(origin, destination, departure_date, return_date):
    """Search lowest roundtrip fare using Amadeus."""
    try:
        token = get_amadeus_token()
    except Exception as e:
        return {"Origin": origin, "Destination": destination, "Error": f"Auth error: {e}"}

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
            return {"Origin": origin, "Destination": destination, "Error": f"API error {resp.status_code}"}

        data = resp.json()
        offers = data.get("data", [])
        if not offers:
            return {"Origin": origin, "Destination": destination, "Error": "No fares found"}

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
        return {"Origin": origin, "Destination": destination, "Error": f"Request error: {e}"}


# ============================================================
# K9SAR (Hidden iPhone Tab + Login + GPS/Map via template)
# ============================================================
K9SAR_CODE = os.getenv("K9SAR_CODE", "1234")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

# On Render, you should use a persistent disk and point this to it, e.g.
# K9SAR_DB_PATH=/var/data/k9sar.db
K9SAR_DB_PATH = os.getenv("K9SAR_DB_PATH", "k9sar.db")


def k9sar_db():
    conn = sqlite3.connect(K9SAR_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def k9sar_init_db():
    with k9sar_db() as conn:
        conn.execute(
            """
        CREATE TABLE IF NOT EXISTS k9_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dog_name TEXT NOT NULL,
            notes TEXT,
            start_time TEXT NOT NULL,
            stop_time TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL,
            distance_miles REAL NOT NULL,
            start_lat REAL,
            start_lng REAL,
            end_lat REAL,
            end_lng REAL,
            track_json TEXT,     -- list of {lat,lng,ts}
            weather_json TEXT,   -- raw weather snapshot
            created_at TEXT NOT NULL
        );
        """
        )


k9sar_init_db()


def k9sar_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("k9sar_ok") is True:
            return f(*args, **kwargs)
        return redirect(url_for("k9sar_login", next=request.path))

    return wrapper


@app.route("/K9sar-login", methods=["GET", "POST"])
def k9sar_login():
    error = None
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        if code == K9SAR_CODE:
            session["k9sar_ok"] = True
            # session cookie => lasts until browser/tab is closed
            nxt = request.args.get("next") or "/K9sar"
            return redirect(nxt)
        error = "Incorrect code"
    return render_template("k9sar_login.html", error=error)


@app.get("/K9sar-logout")
def k9sar_logout():
    session.pop("k9sar_ok", None)
    return redirect("/K9sar-login")


@app.get("/K9sar")
@k9sar_required
def k9sar_page():
    # Hidden page – do not link this anywhere in your UI
    return render_template("k9sar.html")


def fetch_weather(lat: float, lng: float):
    """
    Note: Websites cannot read Apple Weather directly.
    Standard approach is phone GPS -> server fetch weather by coords.
    """
    if not OPENWEATHER_API_KEY:
        return {"error": "OPENWEATHER_API_KEY not set"}

    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {"lat": lat, "lon": lng, "appid": OPENWEATHER_API_KEY, "units": "imperial"}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


@app.get("/api/k9sar/sessions")
@k9sar_required
def k9sar_list_sessions():
    limit = int(request.args.get("limit", 50))
    with k9sar_db() as conn:
        rows = conn.execute(
            """
            SELECT id, dog_name, start_time, stop_time, duration_seconds, distance_miles
            FROM k9_sessions
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/k9sar/sessions")
@k9sar_required
def k9sar_create_session():
    data = request.get_json(force=True)

    dog_name = (data.get("dog_name") or "").strip()
    if not dog_name:
        return jsonify({"error": "dog_name is required"}), 400

    start_time = data.get("start_time")
    stop_time = data.get("stop_time")
    duration_seconds = int(data.get("duration_seconds") or 0)
    distance_miles = float(data.get("distance_miles") or 0.0)

    start_lat = data.get("start_lat")
    start_lng = data.get("start_lng")
    end_lat = data.get("end_lat")
    end_lng = data.get("end_lng")

    notes = data.get("notes") or ""
    track = data.get("track") or []

    weather = None
    try:
        if end_lat is not None and end_lng is not None:
            weather = fetch_weather(float(end_lat), float(end_lng))
    except Exception as e:
        weather = {"error": str(e)}

    now = datetime.now(timezone.utc).isoformat()

    with k9sar_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO k9_sessions
            (dog_name, notes, start_time, stop_time, duration_seconds, distance_miles,
             start_lat, start_lng, end_lat, end_lng, track_json, weather_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dog_name,
                notes,
                start_time,
                stop_time,
                duration_seconds,
                distance_miles,
                start_lat,
                start_lng,
                end_lat,
                end_lng,
                json.dumps(track),
                json.dumps(weather) if weather is not None else None,
                now,
            ),
        )
        session_id = cur.lastrowid

    return jsonify({"ok": True, "id": session_id})


# ==============================
# HOME PAGE
# ==============================
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


# ==============================
# FLIGHT STATUS PAGE
# ==============================
@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    global last_status_results

    company = (request.args.get("company") or "").strip().lower()
    if not is_company_allowed(company):
        flash("Access denied.")
        return redirect(url_for("home"))

    flight_info = None
    uploaded_results = None

    if request.method == "POST":
        # Branch 1: Excel upload
        if "file" in request.files and request.files["file"].filename:
            file = request.files["file"]

            try:
                if file.filename.lower().endswith(".csv"):
                    df = pd.read_csv(file)
                else:
                    df = pd.read_excel(file, sheet_name=0)

                df.columns = df.columns.str.strip().str.lower()
                required_cols = ["airline", "flightnumber", "departure", "arrival"]
                missing = [c for c in required_cols if c not in df.columns]
                if missing:
                    flash(f"Missing columns in file: {missing}")
                    return render_template(
                        "flight_status.html",
                        company=company,
                        flight_info=None,
                        uploaded_results=None,
                    )

                rows = []
                for _, row in df.iterrows():
                    airline = str(row["airline"]).strip().upper()
                    flight_number = str(row["flightnumber"]).strip()
                    dep = str(row["departure"]).strip().upper()
                    arr = str(row["arrival"]).strip().upper()

                    api_result = fetch_status_flightapi(airline, flight_number, dep, arr)

                    rows.append(
                        {
                            "Airline": airline,
                            "FlightNumber": flight_number,
                            "From": dep,
                            "To": arr,
                            "Status": api_result["status"],
                            "EstimatedDeparture": api_result["estimated_departure"],
                            "EstimatedArrival": api_result["estimated_arrival"],
                        }
                    )

                last_status_results = rows
                uploaded_results = rows

            except Exception as e:
                flash(f"Error processing file: {e}")
                return render_template(
                    "flight_status.html",
                    company=company,
                    flight_info=None,
                    uploaded_results=None,
                )

        else:
            # Branch 2: Manual lookup
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

            api_result = fetch_status_flightapi(airline, flight_number, departure, arrival)

            flight_info = {
                "Airline": airline,
                "FlightNumber": flight_number,
                "From": departure,
                "To": arrival,
                "Status": api_result["status"],
                "EstimatedDeparture": api_result["estimated_departure"],
                "EstimatedArrival": api_result["estimated_arrival"],
            }

            last_status_results = [flight_info]

    return render_template(
        "flight_status.html",
        company=company,
        flight_info=flight_info,
        uploaded_results=uploaded_results,
    )


# ==============================
# DOWNLOAD STATUS RESULTS
# ==============================
@app.route("/download-status")
def download_status():
    if not last_status_results:
        flash("No results to download.")
        return redirect(url_for("home"))

    df = pd.DataFrame(last_status_results)
    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(
        output,
        download_name="flight_status_results.xlsx",
        as_attachment=True,
    )


# ==============================
# FLIGHT ANALYSIS (Amadeus)
# ==============================
@app.route("/flight-analysis", methods=["GET", "POST"])
def flight_analysis():
    global last_analysis_results

    company = (request.args.get("company") or "").strip().lower()
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
            flash("Please select both outbound and return dates.")
            return render_template("flight_analysis.html", company=company, analysis_results=None)

        results = []
        for origin in origins:
            for dest in destinations:
                r = search_lowest_fare_amadeus(origin, dest, outbound_date, return_date)
                results.append(
                    {
                        "Origin": origin,
                        "Destination": dest,
                        "DepartureDate": outbound_date,
                        "ReturnDate": return_date,
                        "Price": r.get("Price"),
                        "Currency": r.get("Currency", "USD"),
                        "Error": r.get("Error", ""),
                    }
                )

        analysis_results = results
        last_analysis_results = results

    return render_template("flight_analysis.html", company=company, analysis_results=analysis_results)


@app.route("/download-analysis")
def download_analysis():
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


if __name__ == "__main__":
    app.run(debug=True)
