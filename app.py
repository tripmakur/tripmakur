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
            return {
                "Origin": origin,
                "Destination": destination,
                "Error": f"API error {resp.status_code}",
            }

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
# K9SAR (Hidden iPhone Tab + Login + GPS/Map via templates)
# ============================================================
K9SAR_CODE = os.getenv("K9SAR_CODE", "1234")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

# On Render, attach a persistent disk and set:
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


def k9

