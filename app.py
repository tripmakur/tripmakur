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
def flight_status():
    global last_results
    flight_info = None
    uploaded_results = last_results

    # Manual flight lookup (form has airline, flight_number, from, to)
    if request.method == "POST" and request.form.get("form_type") == "manual":
        airline = (request.form.get("airline") or "").strip().upper()
        flight_number = (request.form.get("flight_number") or "").strip()
        departure = (request.form.get("departure") or "").strip().upper()
        arrival = (request.form.get("arrival") or "").strip().upper()

        if not airline or not flight_number:
            flash("Please provide at least airline code and flight number for manual lookup.")
        else:
            status = fetch_status(airline, flight_number, departure, arrival)
            flight_info = {
                "Airline": airline,
                "FlightNumber": f"{airline}{flight_number}",
                "From": departure or "Unknown",
                "To": arrival or "Unknown",
                "Status": status
            }

    return render_template("flight_status.html", flight_info=flight_info, uploaded_results=uploaded_results)

@app.route("/upload", methods=["POST"])
def upload_file():
    global last_results

    if "file" not in request.files:
        flash("No file part in request.")
        return render_template("flight_status.html", flight_info=None, uploaded_results=None)

    file = request.files["file"]

    if file.filename == "":
        flash("No file selected.")
        return render_template("flight_status.html", flight_info=None, uploaded_results=None)

    filename = file.filename.lower()
    if not allowed_file(filename):
        flash("Invalid file type. Please upload an .xlsx or .csv file.")
        return render_template("flight_status.html", flight_info=None, uploaded_results=None)

    try:
        import openpyxl
        import io

        if filename.endswith(".csv"):
            df = pd.read_csv(file)
        else:
            # Reset file pointer
            file.seek(0)
            workbook = openpyxl.load_workbook(file, data_only=True)
            sheet = workbook.active

            # Find first non-empty row (header row)
            header_row = None
            for i, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                if any(cell not in (None, "", " ") for cell in row):
                    header_row = i
                    break

            file.seek(0)
            df = pd.read_excel(file, sheet_name=0, header=header_row - 1 if header_row else 0)

        # Drop empty rows
        df = df.dropna(how="all")

        # Drop unnamed/index columns
        df = df.loc[:, ~df.columns.str.contains('^unnamed', case=False)]

        # Normalize column names
        df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]

    except Exception as e:
        flash(f"Error reading Excel/CSV file: {e}")
        return render_template("flight_status.html", flight_info=None, uploaded_results=None)

    # Flexible header mapping
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
        return render_template("flight_status.html", flight_info=None, uploaded_results=None)

    # Clean up values
    normalized_df = normalized_df.dropna(how="all")
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
            "FlightNumber": f"{airline}{flight_number}",
            "From": departure,
            "To": arrival,
            "Status": status
        })

    if not results:
        flash("No valid flight rows found in uploaded file.")
        return render_template("flight_status.html", flight_info=None, uploaded_results=None)

    last_results = results
    return render_template("flight_status.html", flight_info=None, uploaded_results=results)



@app.route("/download", methods=["GET"])
def download_excel():
    global last_results
    if not last_results:
        flash("No flight data to download.")
        return render_template("flight_status.html", flight_info=None, uploaded_results=None)

    df = pd.DataFrame(last_results)
    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)
    return send_file(output, download_name="flight_statuses.xlsx", as_attachment=True, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)




