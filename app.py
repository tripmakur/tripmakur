import os
import pandas as pd
import requests
from flask import Flask, render_template, request, flash, send_file
from io import BytesIO

app = Flask(__name__)
app.secret_key = "tripmakur-secret"

API_KEY = os.getenv("API_KEY")  # AviationStack API key from Render environment
if not API_KEY:
    raise ValueError("API_KEY environment variable not set!")

# In-memory storage for last uploaded results
last_results = []

ALLOWED_EXTENSIONS = {"xlsx"}

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    flight_info = None
    uploaded_results = last_results  # Show last uploaded results if any

    # Manual flight lookup
    if request.method == "POST" and "airline" in request.form:
        airline = request.form.get("airline")
        flight_number = request.form.get("flight_number")

        try:
            url = f"http://api.aviationstack.com/v1/flights?access_key={API_KEY}&airline_iata={airline}&flight_iata={flight_number}"
            res = requests.get(url, timeout=10)
            res.raise_for_status()
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
        except Exception as e:
            flash(f"Error fetching flight: {e}")
            flight_info = None

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

    if not allowed_file(file.filename):
        flash("Invalid file type. Please upload an .xlsx file.")
        return render_template("flight_status.html", flight_info=None, uploaded_results=None)

    try:
        df = pd.read_excel(file)
    except Exception as e:
        flash(f"Error reading Excel file: {e}")
        return render_template("flight_status.html", flight_info=None, uploaded_results=None)

    required_cols = {"Airline", "FlightNumber", "From", "To"}
    if not required_cols.issubset(df.columns):
        flash("Excel file must have columns: Airline, FlightNumber, From, To")
        return render_template("flight_status.html", flight_info=None, uploaded_results=None)

    results = []
    for _, row in df.iterrows():
        airline = str(row["Airline"]).strip()
        flight_number = str(row["FlightNumber"]).strip()
        departure = str(row["From"]).strip()
        arrival = str(row["To"]).strip()

        try:
            url = f"http://api.aviationstack.com/v1/flights?access_key={API_KEY}&airline_iata={airline}&flight_iata={flight_number}&dep_iata={departure}&arr_iata={arrival}"
            res = requests.get(url, timeout=10)
            res.raise_for_status()
            data = res.json()
            if "data" in data and data["data"]:
                f = data["data"][0]
                status = f["flight_status"]
            else:
                status = "Not Found"
        except Exception as e:
            status = f"Error: {e}"

        results.append({
            "Airline": airline,
            "FlightNumber": flight_number,
            "From": departure,
            "To": arrival,
            "Status": status
        })

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

    return send_file(
        output,
        download_name="flight_statuses.xlsx",
        as_attachment=True,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
