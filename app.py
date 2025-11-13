from flask import Flask, render_template, request, flash, redirect, url_for, send_file
import pandas as pd
import openpyxl
from io import BytesIO
import logging
import requests

# -----------------------
# App setup
# -----------------------
app = Flask(__name__)
app.secret_key = "your_secret_key"  # Replace with a secure key
last_results = []

# Configure logging
logging.basicConfig(level=logging.INFO)

# -----------------------
# AviationStack API helper
# -----------------------
API_KEY = "YOUR_AVIATIONSTACK_API_KEY"

def fetch_status(airline_code, flight_number, departure=None, arrival=None):
    """
    Fetch flight status from AviationStack API.
    If departure/arrival are not provided, only uses IATA flight code.
    """
    try:
        flight_iata = f"{airline_code}{flight_number}"
        url = f"http://api.aviationstack.com/v1/flights?access_key={API_KEY}&flight_iata={flight_iata}"
        response = requests.get(url)
        data = response.json()
        if "data" not in data or not data["data"]:
            return "Not Found"
        flight = data["data"][0]
        return flight.get("flight_status", "Not Found")
    except Exception as e:
        return f"Error: {e}"

# -----------------------
# Routes
# -----------------------
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    """
    Manual flight lookup form
    """
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
    """
    Upload Excel or CSV with multiple flights
    """
    global last_results

    if "file" not in request.files:
        flash("No file part in request.")
        return render_template("flight_status.html")

    file = request.files["file"]
    if file.filename == "":
        flash("No file selected.")
        return render_template("flight_status.html")

    filename = file.filename.lower()
    if not (filename.endswith(".xlsx") or filename.endswith(".csv")):
        flash("Invalid file type. Please upload .xlsx or .csv.")
        return render_template("flight_status.html")

    try:
        # Load Excel or CSV
        if filename.endswith(".csv"):
            df = pd.read_csv(file)
        else:
            file.seek(0)
            workbook = openpyxl.load_workbook(file, data_only=True)
            sheet = workbook.active
            all_rows = list(sheet.iter_rows(values_only=True))

            # Detect first non-empty row for header
            header_index = None
            for i, row in enumerate(all_rows):
                if any(cell not in (None, "", " ") for cell in row):
                    header_index = i
                    break
            if header_index is None:
                flash("No header row found in Excel file.")
                return render_template("flight_status.html")

            headers = [str(h).strip() if h else "" for h in all_rows[header_index]]
            data_rows = all_rows[header_index + 1:]
            df = pd.DataFrame(data_rows, columns=headers)

        # Drop fully empty rows and unnamed columns
        df = df.dropna(how="all")
        df = df.loc[:, ~df.columns.astype(str).str.match("Unnamed", case=False)]

        # Map uploaded columns to internal names
        column_mapping = {
            "airline": "Airline",
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

        normalized_columns = {}
        for col in df.columns:
            col_lower = col.strip().lower()
            if col_lower in column_mapping:
                normalized_columns[column_mapping[col_lower]] = df[col]

        normalized_df = pd.DataFrame(normalized_columns)

        logging.info(f"📄 Uploaded Excel detected columns: {list(normalized_df.columns)}")

        # Check required columns
        required = {"Airline", "FlightNumber", "From", "To"}
        missing = required - set(normalized_df.columns)
        if missing:
            logging.error(f"❌ Missing columns: {missing}")
            flash(f"Missing required columns. Found: {', '.join(normalized_df.columns)}. Required: Airline, FlightNumber, From, To")
            return render_template("flight_status.html")

    except Exception as e:
        logging.exception("❌ Error reading uploaded file")
        flash(f"Error reading Excel/CSV file: {e}")
        return render_template("flight_status.html")

    # Process flight data
    results = []
    for _, row in normalized_df.iterrows():
        airline = str(row["Airline"]).strip().upper()
        flight_number = str(row["FlightNumber"]).strip()
        departure = str(row["From"]).strip().upper()
        arrival = str(row["To"]).strip().upper()

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

    last_results = results
    return render_template("flight_status.html", uploaded_results=results, flight_info=None)

@app.route("/download-excel")
def download_excel():
    """
    Download last uploaded or manual flight results as Excel
    """
    if not last_results:
        flash("No results available to download.")
        return redirect(url_for("flight_status"))

    df = pd.DataFrame(last_results)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="FlightStatus")
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name="flight_status_results.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

# -----------------------
# Run app
# -----------------------
if __name__ == "__main__":
    app.run(debug=True)






