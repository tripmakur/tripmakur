import os
import pandas as pd
import requests
from flask import Flask, render_template, request, redirect, url_for, send_file, flash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = "tripmakur-secret"

API_KEY = os.getenv("API_KEY")  # AviationStack API key (set in Render)
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {"xlsx"}

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/flight-status", methods=["GET", "POST"])
def flight_status():
    if request.method == "POST":
        airline = request.form.get("airline")
        flight_number = request.form.get("flight_number")

        url = f"http://api.aviationstack.com/v1/flights?access_key={API_KEY}&airline_iata={airline}&flight_iata={flight_number}"
        res = requests.get(url)
        data = res.json()

        flight_info = None
        if "data" in data and data["data"]:
            f = data["data"][0]
            flight_info = {
                "airline": f["airline"]["name"],
                "flight_number": f["flight"]["iata"],
                "departure": f["departure"]["airport"],
                "arrival": f["arrival"]["airport"],
                "status": f["flight_status"],
            }

        return render_template("flight_status.html", flight_info=flight_info)

    return render_template("flight_status.html", flight_info=None)


@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        flash("No file part")
        return redirect(url_for("flight_status"))

    file = request.files["file"]

    if file.filename == "":
        flash("No selected file")
        return redirect(url_for("flight_status"))

    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(file_path)

        try:
            df = pd.read_excel(file_path)
        except Exception as e:
            flash(f"Error reading Excel file: {e}")
            return redirect(url_for("flight_status"))

        # Ensure required columns exist
        required_cols = {"Airline", "FlightNumber", "From", "To"}
        if not required_cols.issubset(df.columns):
            flash("Excel file must have columns: Airline, FlightNumber, From, To")
            return redirect(url_for("flight_status"))

        # Add Status column
        statuses = []
        for _, row in df.iterrows():
            airline = str(row["Airline"]).strip()
            flight_number = str(row["FlightNumber"]).strip()

            url = f"http://api.aviationstack.com/v1/flights?access_key={API_KEY}&airline_iata={airline}&flight_iata={flight_number}"
            res = requests.get(url)
            data = res.json()

            if "data" in data and data["data"]:
                f = data["data"][0]
                statuses.append(f["flight_status"])
            else:
                statuses.append("Not Found")

        df["Status"] = statuses

        output_path = os.path.join(app.config["UPLOAD_FOLDER"], f"updated_{filename}")
        df.to_excel(output_path, index=False)

        return send_file(output_path, as_attachment=True)

    flash("Invalid file type. Please upload an .xlsx file.")
    return redirect(url_for("flight_status"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
