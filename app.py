import os
import logging
import requests
import pandas as pd
from flask import Flask, render_template, request, send_file, flash, redirect, url_for
from werkzeug.utils import secure_filename
import io

# --- App setup ---
app = Flask(__name__)
app.secret_key = "supersecretkey"
app.config["UPLOAD_FOLDER"] = "uploads"
ALLOWED_EXTENSIONS = {"xlsx", "csv"}

# --- Logging setup ---
logging.basicConfig(level=logging.INFO)

# --- API key ---
API_KEY = os.environ.get("AVIATIONSTACK_API_KEY")

# --- Global storage for results ---
last_results = []


# --- Helper Functions ---
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def fetch_status(airline, flight_number, departure, arrival):
    """Fetch flight status from AviationStack API with error logging."""
    if not API_KEY:
        logging.error("❌ AviationStack API key not configured in environment variables.")
        return "API key not configured"

    url = "http://api.aviationstack.com/v1/flights"
    params = {
        "access_key": API_KEY,
        "airline_iata": airline,
        "flight_iata": f"{airline}{flight_number}",
        "dep_iata": departure,
        "arr_iata": arrival,
    }

    try:
        logging.info(f"🔍 Checking flight: {airline}{flight_number} ({departure} → {arrival})")

        response = requests.get(url, params=params, timeout=10)
        data = response.json()

        if "error" in data:
            msg = data["error"].get("message", "Unknown error")
            logging.error(f"⚠️ API error: {msg}")
            return f"API error: {msg}"

        if "data" in data and data["data"]:
            status = data["data"][0].get("flight_status", "Unknown")
            logging.info(f"✅ Flight {airline}{flight_number} status: {status}")
            return status.capitalize()
        else:
            logging.warning(f"🚫 Flight not found: {airline}{flight_number}")
            return "Not Found"

    except requests.exceptions.RequestException as e:
        logging.exception("🌐 Network error while fetching flight status")
        return f"Network error: {e}"

    except Exception as e:
        logging.exception("❗ Unexpected error in fetch_status()")
        return f"Error: {e}"


# --- Routes ---
@app.route("/")
def home():
    return render_template("index.html")


@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    global last_results
    if request.method == "POST":
        airline = request.form.get("airline", "").strip().upper()
        flight_number = request.form.get("flight_number", "").strip()
        departure = request.form.get("departure", "").strip().upper()
        arrival = request.form.get("arrival", "").strip().upper()

        if not all([airline, flight_number, departure, arrival]):
            flash("All fields are required.")
            return render_template("flight_status.html", flight_info=None, uploaded_results=None)

        status = fetch_status(airline, flight_number, departure, arrival)
        flight_info = {
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": departure,
            "To": arrival,
            "Status": status,
        }

        last_results = [flight_info]
        return render_template("flight_status.html", flight_info=flight_info, uploaded_results=None)

    return render_template("flight_status.html", flight_info=None, uploaded_results=None)


@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results

    if "file" not in request.files:
        flash("No file part in request.")
        return render_template("flight_status.html")

    file = request.files["file"]
    if file.filename == "":
        flash("No file selected.")
        return render_template("flight_status.html")

    filename = file.filename.lower()
    if not allowed_file(filename):
        flash("Invalid file type. Please upload .xlsx or .csv.")
        return render_template("flight_status.html")

    try:
        import openpyxl

        if filename.endswith(".csv"):
            df = pd.read_csv(file)
        else:
            file.seek(0)
            workbook = openpyxl.load_workbook(file, data_only=True)
            sheet = workbook.active

            # Detect first header row
            header_row = None
            for i, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                if any(cell not in (None, "", " ") for cell in row):
                    header_row = i
                    break

            file.seek(0)
            df = pd.read_excel(file, sheet_name=0, header=header_row - 1 if header_row else 0)

        df = df.dropna(how="all")
        df = df.loc[:, ~df.columns.str.contains("^unnamed", case=False)]
        df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]

    except Exception as e:
        flash(f"Error reading Excel/CSV file: {e}")
        return render_template("flight_status.html")

    # Column mapping
    column_map = {
        "airline": "Airline",
        "airlinecode": "Airline",
        "carrier": "Airline",
        "flightnumber": "FlightNumber",
        "flightno": "FlightNumber",
        "flight": "FlightNumber",
        "from": "From",
        "departure": "From",
        "dep": "From",
        "origin": "From",
        "to": "To",
        "arrival": "To",
        "arr": "To",
        "destination": "To",
    }

    normalized_df = pd.DataFrame()
    for key, target in column_map.items():
        for col in df.columns:
            if col.startswith(key):
                normalized_df[target] = df[col]
                break

    required = {"Airline", "FlightNumber", "From", "To"}
    missing = required - set(normalized_df.columns)
    if missing:
        flash(f"Missing required columns. Found columns: {', '.join(df.columns)}. Required: Airline, FlightNumber, From, To")
        return render_template("flight_status.html")

    results = []
    for _, row in normalized_df.iterrows():
        airline = str(row.get("Airline", "")).strip().upper()
        flight_number = str(row.get("FlightNumber", "")).strip()
        departure = str(row.get("From", "")).strip().upper()
        arrival = str(row.get("To", "")).strip().upper()

        if not airline or not flight_number:
            continue

        status = fetch_status(airline, flight_number, departure, arrival)
        results.append({
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": departure,
            "To": arrival,
            "Status": status
        })

    if not results:
        flash("No valid flight rows found in uploaded file.")
        return render_template("flight_status.html")

    last_results = results
    return render_template("flight_status.html", uploaded_results=results)


@app.route("/download", methods=["GET"])
def download_results():
    global last_results
    if not last_results:
        flash("No flight data available for download.")
        return redirect(url_for("flight_status"))

    df = pd.DataFrame(last_results)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="FlightStatus")

    output.seek(0)
    return send_file(output, as_attachment=True, download_name="flight_status_results.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# --- Run app locally ---
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)





