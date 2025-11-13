from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
app.secret_key = "test_secret"

ALLOWED_COMPANIES = ["CompanyA", "CompanyB"]

@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        print(request.form)  # Debug: prints submitted data
        company = request.form.get("company", "").strip()
        if not company:
            flash("Enter a company")
            return render_template("index.html")
        if company not in ALLOWED_COMPANIES:
            flash(f"Access denied: {company}")
            return render_template("index.html")
        return redirect(url_for("flight_status", company=company))
    return render_template("index.html")

@app.route("/flight-status")
def flight_status():
    company = request.args.get("company")
    return f"Flight status page for company: {company}"

if __name__ == "__main__":
    app.run(debug=True)












