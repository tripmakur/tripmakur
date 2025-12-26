import os
import json
import time
import sqlite3
from datetime import datetime
from functools import wraps
from io import BytesIO

import requests
import pandas as pd
from flask import (
    Flask, render_template, request, redirect,
    url_for, jsonify, send_file, session
)

# =====================================================
# App setup
# =====================================================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "k9sar-secret")

@app.get("/")
def root():
    return render_template("index.html")


# =====================================================
# K9SAR CONFIG
# =====================================================
K9SAR_ACCESS_CODE = os.getenv("K9SAR_ACCESS_CODE", "k9secure")

# Force free-tier-safe DB path
K9SAR_DB_PATH = os.getenv("K9SAR_DB_PATH") or "/tmp/k9sar.db"
if K9SAR_DB_PATH.startswith("/var/data"):
    K9SAR_DB_PATH = "/tmp/k9sar.db"

OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

# =====================================================
# DB helpers
# =====================================================
def k9sar_db():
    parent = os.path.dirname(K9SAR_DB_PATH)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except PermissionError:
            pass
    conn = sqlite3.connect(K9SAR_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def k9sar_init_db():
    with k9sar_db() as conn:
        conn.execute("""
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
        """)
        conn.commit()


k9sar_init_db()

# =====================================================
# Auth helpers
# =====================================================
def k9sar_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("k9sar_ok"):
            return f(*args, **kwargs)
        return redirect(url_for("k9sar_login"))
    return wrapper

# =====================================================
# Login / Logout
# =====================================================
@app.route("/K9sar-login", methods=["GET", "POST"])
def k9sar_login():
    error = None
    if request.method == "POST":
        if request.form.get("code") == K9SAR_ACCESS_CODE:
            session["k9sar_ok"] = True
            return redirect("/K9sar")
        error = "Invalid access code"
    return render_template("k9sar_login.html", error=error)


@app.route("/K9sar-logout")
def k9sar_logout():
    session.clear()
    return redirect("/K9sar-login")

# =====================================================
# Main page
# =====================================================
@app.get("/K9sar")
@k9sar_required
def k9sar_page():
    return render_template("k9sar.html")

# =====================================================
# API: sessions
# =====================================================
@app.get("/api/k9sar/sessions")
@k9sar_required
def k9sar_sessions():
    limit = int(request.args.get("limit", 50))
    with k9sar_db() as conn:
        rows = conn.execute("""
            SELECT *,
                   substr(notes,1,80) AS notes_preview
            FROM k9_sessions
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/k9sar/sessions/<int:sid>")
@k9sar_required
def k9sar_session_detail(sid):
    with k9sar_db() as conn:
        r = conn.execute(
            "SELECT * FROM k9_sessions WHERE id=?",
            (sid,)
        ).fetchone()
    if not r:
        return jsonify({"error": "not found"}), 404

    d = dict(r)
    d["track"] = json.loads(d["track_json"] or "[]")
    d["weather"] = json.loads(d["weather_json"] or "{}")
    return jsonify(d)


@app.post("/api/k9sar/sessions")
@k9sar_required
def k9sar_save_session():
    data = request.json or {}
    weather = {}

    if OPENWEATHER_API_KEY and data.get("end_lat"):
        try:
            w = requests.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={
                    "lat": data["end_lat"],
                    "lon": data["end_lng"],
                    "appid": OPENWEATHER_API_KEY,
                    "units": "imperial",
                },
                timeout=10,
            )
            weather = w.json()
        except Exception as e:
            weather = {"error": str(e)}

    with k9sar_db() as conn:
        conn.execute("""
        INSERT INTO k9_sessions
        (dog_name, notes, start_time, stop_time,
         duration_seconds, distance_miles,
         start_lat, start_lng, end_lat, end_lng,
         track_json, weather_json, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
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
            datetime.utcnow().isoformat(),
        ))
        conn.commit()

    return jsonify({"ok": True})

# =====================================================
# EXPORT
# =====================================================
@app.get("/api/k9sar/export.csv")
@k9sar_required
def k9sar_export_csv():
    with k9sar_db() as conn:
        rows = conn.execute("SELECT * FROM k9_sessions").fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    return send_file(
        BytesIO(df.to_csv(index=False).encode()),
        download_name="k9sar_sessions.csv",
        as_attachment=True,
    )


@app.get("/api/k9sar/export.xlsx")
@k9sar_required
def k9sar_export_xlsx():
    with k9sar_db() as conn:
        rows = conn.execute("SELECT * FROM k9_sessions").fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    out = BytesIO()
    df.to_excel(out, index=False)
    out.seek(0)
    return send_file(
        out,
        download_name="k9sar_sessions.xlsx",
        as_attachment=True,
    )

# =====================================================
# PWA files
# =====================================================
@app.get("/static/manifest.webmanifest")
def manifest():
    return send_file("static/manifest.webmanifest")


@app.get("/sw.js")
def sw():
    return send_file("static/sw.js")


if __name__ == "__main__":
    app.run(debug=True)

