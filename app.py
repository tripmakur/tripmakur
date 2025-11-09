from flask import Flask, render_template, request, jsonify
import requests
import os

app = Flask(__name__)

API_KEY = os.getenv("API_KEY", "YOUR_API_KEY_HERE")
API_URL = "http://api.aviationstack.com/v1/flights"

def get_flight_status(airline, flight_number, dep, arr):
    params = {
        "access_key": API_KEY,
        "airline_iata": airline,
        "flight_iata": f"{airline}{flight_number}",
        "dep_iata": dep,
        "arr_iata": arr,
    }
    try:
        r = requests.get(API_URL, params=params, timeout=10)
        data = r.json()
        if "data" in data and data["data"]:
            flight = data["data"][0]
            return {
                "airline": airline,
                "flight": f"{airline}{flight_number}",
                "departure": dep,
                "arrival": arr,
                "status": flight.get("flight_status", "Unknown").capitalize()
            }
        return {"airline": airline, "flight": f"{airline}{flight_number}",
                "departure": dep, "arrival": arr, "status": "Not Found"}
    except Exception as e:
        return {"airline": airline, "flight": f"{airline}{flight_number}",
                "departure": dep, "arrival": arr, "status": f"Error: {e}"}


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/flight-status")
def flight_status_page():
    return render_template("flight_status.html")


@app.route("/check_status", methods=["POST"])
def check_status():
    data = request.get_json()
    result = get_flight_status(
        data["airline"].strip().upper(),
        data["flight_number"].strip(),
        data["departure"].strip().upper(),
        data["arrival"].strip().upper(),
    )
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True)
