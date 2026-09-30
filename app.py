import os
import hmac
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, send_from_directory, session, url_for


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output" / "web_jobs"
GENERATOR = BASE_DIR / "weekly_aqi_report.py"
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

app = Flask(__name__)
app.secret_key = os.environ.get("AQI_SECRET_KEY") or os.urandom(32)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))


@app.before_request
def require_access_code():
    if (os.environ.get("AQI_ACCESS_CODE") and request.endpoint not in {"login", "static"}
            and not session.get("authorized")):
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        expected = os.environ.get("AQI_ACCESS_CODE", "")
        supplied = request.form.get("access_code", "")
        if expected and hmac.compare_digest(supplied, expected):
            session["authorized"] = True
            return redirect(url_for("index"))
        return render_template("login.html", error="That access code did not match."), 401
    return render_template("login.html")


def job_directory(job_id):
    try:
        normalized_id = str(uuid.UUID(job_id))
    except (ValueError, AttributeError):
        abort(404)
    return OUTPUT_DIR / normalized_id


def cleanup_old_jobs():
    cutoff = time.time() - 24 * 60 * 60
    if not OUTPUT_DIR.exists():
        return
    for path in OUTPUT_DIR.iterdir():
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
        except OSError:
            continue


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/generate")
def generate():
    upload = request.files.get("csv_file")
    if upload is None or not upload.filename:
        return render_template("index.html", error="Choose a CSV file first."), 400
    if Path(upload.filename).suffix.lower() != ".csv":
        return render_template("index.html", error="Upload a .csv file."), 400

    cleanup_old_jobs()
    job_id = str(uuid.uuid4())
    job_dir = job_directory(job_id)
    job_dir.mkdir(parents=True, exist_ok=False)
    input_path = job_dir / "input.csv"
    upload.save(input_path)

    command = [
        sys.executable,
        str(GENERATOR),
        "--input",
        str(input_path),
        "--output-dir",
        str(job_dir),
    ]
    week_start = request.form.get("week_start", "").strip()
    if week_start:
        command.extend(["--start", week_start])

    try:
        result = subprocess.run(
            command,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired:
        input_path.unlink(missing_ok=True)
        return render_template(
            "index.html", error="Report generation took too long. Try a smaller file."
        ), 504
    finally:
        input_path.unlink(missing_ok=True)

    if result.returncode != 0:
        details = (result.stderr or result.stdout or "The report generator stopped unexpectedly.")[-2500:]
        return render_template("index.html", error=details), 422

    pdf_files = sorted(job_dir.glob("Weekly_AQI_Report_*.pdf"))
    excel_files = sorted(job_dir.glob("Weekly_AQI_Stats_*.xlsx"))
    if not pdf_files or not excel_files:
        return render_template(
            "index.html", error="The generator finished without producing both report files."
        ), 500

    return render_template(
        "index.html",
        result={
            "job_id": job_id,
            "pdf_name": pdf_files[0].name,
            "excel_name": excel_files[0].name,
        },
    )


@app.get("/download/<job_id>/<file_kind>")
def download(job_id, file_kind):
    job_dir = job_directory(job_id)
    patterns = {
        "pdf": "Weekly_AQI_Report_*.pdf",
        "excel": "Weekly_AQI_Stats_*.xlsx",
    }
    if file_kind not in patterns:
        abort(404)
    matches = list(job_dir.glob(patterns[file_kind]))
    if not matches:
        abort(404)
    return send_from_directory(job_dir, matches[0].name, as_attachment=True)


@app.errorhandler(413)
def upload_too_large(_error):
    return render_template("index.html", error="The file is larger than the 100 MB limit."), 413


if __name__ == "__main__":
    app.run(
        host=os.environ.get("AQI_HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )