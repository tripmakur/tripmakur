import os
import pandas as pd
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from io import BytesIO

app = Flask(__name__)
app.secret_key = "tripmakur-secret"

# Read API key from environment
API_KEY = os.getenv("API_KEY", "")

# In-memory storage for last uploaded results
last_results = []

ALLOWED_EXTENSIONS = {"xlsx", "csv"}

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def fetch_status(airline, flight_number, departure="", arrival=""):
    if not API_KEY:
        return "API_KEY not configured"
    try:
        params = {
            "access_key": API_KEY,
            "airline_iata": airline,
            "flight_iata": f"{airline}{flight_number}",
        }
        if departure:
            params["dep_iata"] = departure
        if arrival:
            params["arr_iata"] = arrival
        res = requests.get("http://api.aviationstack.com/v1/flights", params=params, timeout=12)
        res.raise_for_status()
        data = res.json()
        if "data" in data and data["data"]:
            f = data["data"][0]
            return f.get("flight_status", "Unknown")
        return "Not Found"
    except Exception as e:
        return f"Error: {e}"

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/flight-status", methods=["GET", "POST"])

