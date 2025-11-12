import os
import pandas as pd
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from werkzeug.utils import secure_filename
from io import BytesIO

app = Flask(__name__)
app.secret_key = "tripmakur-secret"

API_KEY = os.getenv("API_KEY")  # AviationStack API key set in Render
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXTENSIONS = {"xlsx"}

# In-memory storage for last uploaded results
last_results = []

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    flight_info = None
    uploaded_results = last_results  # show previous results if any

    # Manual flight lookup
    if request.method == "POST" and "airline" in request.form:
        airline = request.form.get("airline")
        flight_number = request.form.get("flight_number")

        url = f"http://api.aviationstack.com/v1/flights?access_key={API_KEY}&airline_iata={airline}&flight_iata={flight_number}"
        res = requests.get(url)
        data = res.json()

        if "data" in data and data["data"]:
            f = data["data"][0]
            flight_info = {
                "airline": f["airline"]["name"],
                "flight_number": f["flight"]["iata"],
                "departure": f["departure"]["airport"],
                "arrival": f["arrival"]["airport"],
                "status": f["flight_status"],
            }
        else:
            flight_info = {
                "airline": airline,
                "flight_number": flight_number,
                "departure": "Unknown",
                "arrival": "Unknown",
                "status": "Not Found"
            }

    return render_template("flight_status.html", flight_info=flight_info, uploaded_results=uploaded_results)


@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results
    if "file" not in request.files:
        flash("No file part")
        return render_template("flight_status.html", flight_info=None, uploaded_results=None)

    file = request.files["file"]

    if file.filename == "":
        flash("No selected file")
        return render_template("flight_status.html", flight_info=None, uploaded_results=None)

    if file and allowed_file(file.filename):
        try:
            df = pd.read_excel(file)
        except Exception as e:
            flash(f"Error reading Excel file: {e}")
            return ren
