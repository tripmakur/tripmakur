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

# ============================================================
# APP SETUP
# ============================================================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me")

# ============================================================
# ALLOWED COMPANIES
# ============================================================
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
last_status_results = []
last_analysis_results = []


def is_company_allowed(company: str) -> bool:
    if not ALLOWED_COMPANIES:
        return True
    return bool(company) and company.lower() in ALLOWED_COMPANIES


# ============================================================
# FLIGHT STATUS (FlightAPI)
# ============================================================
FLIGHTAPI_KEY = os.getenv("FLIGHTAPI_KEY")


def map_status_code(code):
    return {
        1: "Scheduled",
        2: "Arrived",
        3: "Departed",
        4: "Delayed",
        5: "Cancelled",
    }.get(code, "Unknown")


def fetch_status_flightapi(airline, flight_number, dep=None, arr=None):
    central = pytz.timezone("America/Chicago")
    date_param = datetime.now(central).strftime("%Y%m%d")

    url = (
        f"https://api.flightapi.io/airline/{FLIGHTAPI_KEY}"
        f"?num={flight_number}&name={airline.lower()}&date={date_param}"
    )

    try:
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            return {"status": "API error", "estimated_departure": None, "estimated_arrival": None}

        data = r.json()
        flights = data.get("flights", [])
        if not flights:
            return {"status": "Not Found", "estimated_departure": None, "estimated_arrival": None}

        f = flights[0]
        return {
            "status": f.get("displayStatus") or map_status_code(f.get("status")),
            "estimated_departure": f.get("departureTime"),
            "estimated_arrival": f.get("arrivalTime"),
        }

    except Exception as e:
        return {"status": f"Error: {e}", "estimated_departure": None, "estimated_arrival": None}


# ============================================================
# AMADEUS (ANALYSIS)
# ============================================================
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")

AMADEUS_AUTH_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
AMADEUS_FLIGHT_OFFERS_URL = "https://test.api.amadeus.com/v2/shopping/flight-offers"

amadeus_token = None
amadeus_expiry = 0


def get_amadeus_token():
    global amadeus_token, amadeus_expiry
    if amadeus_token and time.time() < amadeus_expiry:
        return amadeus_token

    r = requests.post(
        AMADEUS_AUTH_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": AMADEUS_CLIENT_ID,
            "client_secret": AMADEUS_CLIENT_SECRET,
        },
        timeout=20,
    )
    payload = r.json()
    amadeus_token = payload["access_token"]
    amadeus_expiry = time.time() + payload.get("expires_in", 1800) - 60
    return amadeus_token


# ============================================================
# K9SAR (HIDDEN TOOL)
# ============================================================
K9SAR_CODE = os.getenv("K9SAR_CODE", "1234")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
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
                track_json TEXT,
                weather_json TEXT,
                created_at TEXT NOT NULL
            );
            """
        )


k9sar_init_db()


def k9sar_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("k9sar_ok"):
            return f(*args, **kwargs)
        return redirect(url_for("k9sar_login", next=request.path))

    return wrapper


@app.route("/K9sar-login", methods=["GET", "POST"])
def k9sar_login():
    error = None
    if request.method == "POST":
        if request.form.get("code") == K9SAR_CODE:
            session["k9sar_ok"] = True
            return redirect("/K9sar")
        error = "Invalid code"
    return render_template("k9sar_login.html", error=error)


@app.get("/K9sar-logout")
def k9sar_logout():
    session.clear()
    return redirect("/K9sar-login")


@app.get("/K9sar")
@k9sar_required
def k9sar_page():
    return render_template("k9sar.html")


def fetch_weather(lat, lng):
    if not OPENWEATHER_API_KEY:
        return None
    r = requests.get(
        "https://api.openweathermap.org/data/2.5/weather",
        params={"lat": lat, "lon": lng, "appid": OPENWEATHER_API_KEY, "units": "imperial"},
        timeout=10,
    )
    return r.json()


@app.get("/api/k9sar/sessions")
@k9sar_required
def k9sar_sessions():
    with k9sar_db() as conn:
        rows = conn.execute(
            """
            SELECT id, dog_name, start_time, stop_time,
                   duration_seconds, distance_miles,
                   COALESCE(notes,'') AS notes
            FROM k9_sessions
            ORDER BY id DESC
            LIMIT 50
            """
        ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["notes_preview"] = (d["notes"][:60] + "…") if len(d["notes"]) > 60 else d["notes"]
        out.append(d)
    return jsonify(out)


@app.get("/api/k9sar/sessions/<int:sid>")
@k9sar_required
def k9sar_details(sid):
    with k9sar_db() as conn:
        r = conn.execute("SELECT * FROM k9_sessions WHERE id=?", (sid,)).fetchone()
    if not r:
        return jsonify({"error": "Not found"}), 404

    d = dict(r)
    d["track"] = json.loads(d.get("track_json") or "[]")
    d["weather"] = json.loads(d.get("weather_json") or "null")
    return jsonify(d)


@app.post("/api/k9sar/sessions")
@k9sar_required
def k9sar_save():
    data = request.get_json()
    weather = fetch_weather(data.get("end_lat"), data.get("end_lng"))

    with k9sar_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO k9_sessions
            (dog_name, notes, start_time, stop_time, duration_seconds,
             distance_miles, start_lat, start_lng, end_lat, end_lng,
             track_json, weather_json, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data["dog_name"],
                data.get("notes"),
                data["start_time"],
                data["stop_time"],
                data["duration_seconds"],
                data["distance_miles"],
                data.get("start_lat"),
                data.get("start_lng"),
                data.get("end_lat"),
                data.get("end_lng"),
                json.dumps(data.get("track", [])),
                json.dumps(weather),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return jsonify({"ok": True, "id": cur.lastrowid})


# ============================================================
# HOME
# ============================================================
@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        company = request.form.get("company", "").lower()
        if not is_company_allowed(company):
            flash("Access denied.")
            return render_template("index.html")
        return redirect(url_for("flight_status", company=company))
    return render_template("index.html")


# ============================================================
# FLIGHT STATUS PAGE
# ============================================================
@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    global last_status_results
    company = request.args.get("company", "")
    if not is_company_allowed(company):
        return redirect("/")

    flight_info = None
    if request.method == "POST":
        airline = request.form.get("airline", "").upper()
        flight_number = request.form.get("flight_number")
        dep = request.form.get("departure", "").upper()
        arr = request.form.get("arrival", "").upper()

        result = fetch_status_flightapi(airline, flight_number, dep, arr)
        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": dep,
            "To": arr,
            "Status": result["status"],
            "EstimatedDeparture": result["estimated_departure"],
            "EstimatedArrival": result["estimated_arrival"],
        }
        last_status_results = [flight_info]

    return render_template("flight_status.html", company=company, flight_info=flight_info)


# ============================================================
# DOWNLOAD STATUS
# ============================================================
@app.route("/download-status")
def download_status():
    if not last_status_results:
        return redirect("/")
    df = pd.DataFrame(last_status_results)
    buf = BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)
    return send_file(buf, download_name="flight_status.xlsx", as_attachment=True)


# ============================================================
# RUN
# ============================================================
if __name__ == "__main__":
    app.run(debug=True)
