# -*- coding: utf-8 -*-
"""
Sewa Setu - Old Age Pension Portal
Government of Purvanchal, Department of Social Welfare

Developed by: Netlink Infosolutions Pvt Ltd (2023)
Maintenance contract ended 31/01/2026.
"""

import os
import io
import csv
import random
import hashlib
import hmac
import secrets
import threading
import configparser
from datetime import datetime, date, timedelta

import requests
import psycopg2
from psycopg2.pool import ThreadedConnectionPool, PoolError
from werkzeug.security import check_password_hash, generate_password_hash
from flask import (Flask, request, session, redirect, url_for, render_template,
                   flash, send_file, abort, jsonify, g)
from fpdf import FPDF

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config", "app.ini"))

app = Flask(__name__)
APP_ENV = os.environ.get("APP_ENV", "development").lower()
SECRET_KEY = os.environ.get("SECRET_KEY")
if APP_ENV == "production" and (not SECRET_KEY or SECRET_KEY.startswith("REPLACE_") or
                                 SECRET_KEY == "local-development-only-change-me"):
    raise RuntimeError("Set a unique SECRET_KEY in production; do not use the example value")
app.secret_key = SECRET_KEY or secrets.token_hex(32)
app.config.update(
    PERMANENT_SESSION_LIFETIME=1800,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(APP_ENV == "production"),
    MAX_CONTENT_LENGTH=102400,
)

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
if APP_ENV == "production" and (not ADMIN_PASSWORD or ADMIN_PASSWORD.startswith("REPLACE_")):
    raise RuntimeError("Set a unique ADMIN_PASSWORD in production")
if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(24)
    app.logger.warning("Temporary development admin password generated; set ADMIN_PASSWORD explicitly.")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
if APP_ENV == "production" and (not DB_PASSWORD or DB_PASSWORD.startswith("REPLACE_") or
                                 DB_PASSWORD == "local-dev-only-change-me"):
    raise RuntimeError("Set a unique DB_PASSWORD in production")

SMS_GATEWAY_URL = os.environ.get("SMS_GATEWAY_URL", config.get("app", "sms_gateway_url"))
OTP_VALIDITY_SECONDS = config.getint("app", "otp_validity_seconds")
UPLOAD_DIR = config.get("app", "upload_dir")
SCHEME_DEADLINE = datetime.strptime(
    os.environ.get("SCHEME_DEADLINE", config.get("pension", "scheme_deadline")),
    "%Y-%m-%d %H:%M")
MIN_AGE = config.getint("pension", "min_age")
SLA_DAYS = config.getint("pension", "sla_days")

BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]

import logging
_logdir = "/var/log/sewasetu"
try:
    os.makedirs(_logdir, exist_ok=True)
    _fh = logging.FileHandler(os.path.join(_logdir, "sewasetu-app.log"))
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s in app: %(message)s"))
    app.logger.addHandler(_fh)
    app.logger.setLevel(logging.INFO)
except Exception:
    pass


# Gunicorn runs separate worker processes. Initialize one bounded pool lazily
# inside each worker; never create a pool in the master process.
_db_pool = None
_db_pool_lock = threading.Lock()

class PooledConnection:
    """Expose a psycopg2 connection while making close() return it to the pool."""
    def __init__(self, pool, connection):
        self._pool = pool
        self._connection = connection
        self._released = False

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self):
        if not self._released:
            self._released = True
            self._pool.putconn(self._connection, close=self._connection.closed != 0)


def get_db():
    global _db_pool
    if _db_pool is None:
        with _db_pool_lock:
            if _db_pool is None:
                _db_pool = ThreadedConnectionPool(
                    minconn=1, maxconn=int(os.environ.get("DB_POOL_MAX", "4")),
                    host=os.environ.get("DB_HOST", config.get("database", "host")),
                    port=int(os.environ.get("DB_PORT", config.get("database", "port"))),
                    dbname=os.environ.get("DB_NAME", config.get("database", "name")),
                    user=os.environ.get("DB_USER", config.get("database", "user")),
                    password=DB_PASSWORD or config.get("database", "password"),
                    connect_timeout=5,
                    application_name="sewasetu-web",
                )
    try:
        connection = PooledConnection(_db_pool, _db_pool.getconn())
        if not hasattr(g, "_db_connections"):
            g._db_connections = set()
        g._db_connections.add(connection)
        return connection
    except PoolError:
        app.logger.error("database connection pool exhausted")
        raise


@app.teardown_appcontext
def release_db_connections(_error=None):
    # Also release connections on exceptional request paths, not only explicit
    # close() calls. PooledConnection.close() is idempotent.
    for connection in getattr(g, "_db_connections", set()):
        connection.close()


def verify_portal_password(stored, supplied):
    """Verify modern hashes and transparently upgrade legacy SHA-256 hashes."""
    if not stored:
        return False, None
    if stored.startswith(("pbkdf2:", "scrypt:")):
        return check_password_hash(stored, supplied), None
    legacy = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
    if hmac.compare_digest(stored, legacy):
        return True, generate_password_hash(supplied)
    return False, None


def normalize_citizen_name(value):
    """Trim surrounding whitespace without byte-truncating Unicode names."""
    return (value or "").strip()[:100]


def send_sms(mobile, text):
    try:
        response = requests.post(SMS_GATEWAY_URL + "/api/send",
                                 json={"to": mobile, "text": text}, timeout=(2, 4))
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        app.logger.error("sms gateway error: %s" % e)
        return False


def deadline_remaining():
    delta = SCHEME_DEADLINE - datetime.now()
    if delta.total_seconds() <= 0:
        return None
    return int(delta.total_seconds() // 3600)


def new_application_no():
    return "SSP" + datetime.now().strftime("%y") + str(random.randint(100000, 999999))


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if APP_ENV == "production":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.route("/healthz")
def healthz():
    connection = get_db()
    cursor = connection.cursor()
    cursor.execute("SELECT 1")
    cursor.fetchone()
    cursor.close(); connection.close()
    return jsonify({"status": "ok", "database": "ready"}), 200


@app.route("/")
def index():
    return render_template("index.html", hours_left=deadline_remaining())


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/__gateway/", defaults={"subpath": ""})
@app.route("/__gateway/<path:subpath>")
def gateway_proxy(subpath):
    # Convenience proxy to the internal SMS gateway, so the OTP inbox is reachable
    # on the main site without opening a second port on the host. The gateway
    # itself runs only on the internal network.
    try:
        r = requests.get(SMS_GATEWAY_URL + "/" + subpath,
                         params=request.args, timeout=5)
        return (r.content, r.status_code,
                {"Content-Type": r.headers.get("Content-Type", "text/html")})
    except Exception as e:
        return ("SMS gateway unreachable: %s" % e, 502)


# ---------------------------------------------------------------------------
# Application flow: mobile -> OTP -> form steps -> upload -> declaration
# ---------------------------------------------------------------------------

@app.route("/apply", methods=["GET", "POST"])
def apply():
    if deadline_remaining() is None:
        flash("The application window for this scheme has closed.")
        return redirect(url_for("index"))
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        captcha = request.form.get("captcha", "").strip()
        expected_captcha = session.pop("captcha_answer", None)
        if expected_captcha is None or not captcha.isdigit() or int(captcha) != expected_captcha:
            flash("Security check failed. Please try again.")
            return render_template("apply.html", captcha_q=make_captcha())
        if len(mobile) != 10 or not mobile.isdigit():
            flash("Something went wrong. Please try again.")
            return render_template("apply.html", captcha_q=make_captcha())
        code = "%06d" % secrets.randbelow(1000000)
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT created_at FROM otps WHERE mobile = %s ORDER BY id DESC LIMIT 1", (mobile,))
        previous = cur.fetchone()
        if previous and (datetime.now() - previous[0]).total_seconds() < 60:
            cur.close(); conn.close()
            flash("Please wait one minute before requesting another OTP.")
            return render_template("apply.html", captcha_q=make_captcha())
        code_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
        cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                    (mobile, code_hash, datetime.now()))
        conn.commit()
        cur.close(); conn.close()
        if not send_sms(mobile, "Your Sewa Setu OTP is %s. Valid for 5 minutes." % code):
            cleanup = get_db(); cleanup_cur = cleanup.cursor()
            cleanup_cur.execute("DELETE FROM otps WHERE mobile = %s", (mobile,))
            cleanup.commit(); cleanup_cur.close(); cleanup.close()
            flash("SMS delivery is temporarily unavailable. Please try again shortly.")
            return render_template("apply.html", captcha_q=make_captcha())
        session.permanent = True
        session["apply_mobile"] = mobile
        return redirect(url_for("verify"))
    return render_template("apply.html", captcha_q=make_captcha())


def make_captcha():
    a, b = random.randint(1, 9), random.randint(1, 9)
    session["captcha_answer"] = a + b
    return "%d + %d" % (a, b)


@app.route("/verify", methods=["GET", "POST"])
def verify():
    mobile = session.get("apply_mobile")
    if not mobile:
        return redirect(url_for("apply"))
    if request.method == "POST":
        code = request.form.get("otp", "").strip()
        attempts = int(session.get("otp_attempts", 0))
        if attempts >= 5:
            session.pop("apply_mobile", None)
            session.pop("otp_attempts", None)
            flash("Too many incorrect OTP attempts. Please request a new OTP.")
            return redirect(url_for("apply"))
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT code, created_at FROM otps WHERE mobile = %s "
                    "ORDER BY id DESC LIMIT 1", (mobile,))
        row = cur.fetchone()
        submitted_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        if row and hmac.compare_digest(str(row[0]), submitted_hash):
            age = (datetime.now() - row[1]).total_seconds()
            if age > OTP_VALIDITY_SECONDS:
                cur.close(); conn.close()
                app.logger.warning("otp expired mobile=%s age=%ds" % (mobile, int(age)))
                flash("OTP expired. Please request a new OTP.")
                return redirect(url_for("apply"))
            cur.execute("DELETE FROM otps WHERE mobile = %s", (mobile,))
            conn.commit()
            cur.close(); conn.close()
            session.pop("otp_attempts", None)
            session["verified_mobile"] = mobile
            return redirect(url_for("form_step", step=1))
        session["otp_attempts"] = attempts + 1
        flash("Invalid OTP. Please check the code and try again.")
    return render_template("verify.html", mobile=mobile)


@app.route("/form/<int:step>", methods=["GET", "POST"])
def form_step(step):
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if step not in (1, 2, 3):
        abort(404)
    if request.method == "POST":
        data = session.get("form_data", {})
        for k, v in request.form.items():
            data[k] = v
        session["form_data"] = data
        if step < 3:
            return redirect(url_for("form_step", step=step + 1))
        return redirect(url_for("upload"))
    return render_template("form_step%d.html" % step,
                           data=session.get("form_data", {}), blocks=BLOCKS)


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        f = request.files.get("document")
        if f is None or f.filename == "":
            flash("ERR_VAL_47")
            return render_template("upload.html")
        filename = f.filename.lower()
        content = f.read(102401)
        if not filename.endswith(".pdf") or not content.startswith(b"%PDF-"):
            flash("Please upload a valid PDF document.")
            return render_template("upload.html")
        if len(content) > 102400:
            flash("The PDF must be 100 KB or smaller.")
            return render_template("upload.html")
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        path = os.path.join(UPLOAD_DIR, "%s_%s.pdf" %
                            (session["verified_mobile"], secrets.token_hex(16)))
        with open(path, "wb") as out:
            out.write(content)
        session["doc_path"] = path
        return redirect(url_for("declaration"))
    return render_template("upload.html")


@app.route("/declaration", methods=["GET", "POST"])
def declaration():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        return handle_submission()
    return render_template("declaration.html")


def handle_submission():
    # TODO: split this up some day. It grew. - RK, 09/2024
    mobile = session.get("verified_mobile")
    data = session.get("form_data", {})
    doc_path = session.get("doc_path", "")

    # Preserve complete UTF-8 citizen names; the old 20-byte sanitizer
    # truncated Indian-language names and corrupted identity records.
    name = normalize_citizen_name(data.get("applicant_name", ""))
    village = data.get("village", "").strip()
    block = data.get("block", "").strip()
    gender = data.get("gender", "")
    marital = data.get("marital_status", "")
    husband_name = data.get("husband_name", "")
    husband_employer = data.get("husband_employer", "")
    bank_account = data.get("bank_account", "").strip()
    ifsc = data.get("ifsc", "").strip().upper()
    dob_raw = data.get("dob", "").strip()

    if not name or not village or not block or not bank_account or not dob_raw:
        flash("Something went wrong. Please try again.")
        return redirect(url_for("form_step", step=1))

    if marital == "Widowed":
        if not husband_name or not husband_employer:
            flash("Something went wrong. Please try again.")
            return redirect(url_for("form_step", step=1))

    # lenient date parsing to reduce rejections (CR-2024-117)
    dob = None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            dob = datetime.strptime(dob_raw, fmt).date()
            break
        except ValueError:
            continue
    if dob is None:
        flash("Something went wrong. Please try again.")
        return redirect(url_for("form_step", step=1))

    today = date.today()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    if age < MIN_AGE:
        flash("Applicant must be above %d years of age to be eligible." % MIN_AGE)
        return redirect(url_for("form_step", step=1))

    if datetime.now() > SCHEME_DEADLINE:
        flash("The application deadline has passed.")
        return redirect(url_for("index"))

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT count(*) FROM applications WHERE mobile = %s", (mobile,))
    if cur.fetchone()[0] > 0:
        cur.close(); conn.close()
        flash("An application already exists for this mobile number. "
              "Duplicate applications are not permitted.")
        return redirect(url_for("index"))

    app_no = new_application_no()
    cur.execute(
        """INSERT INTO applications
           (application_no, applicant_name, mobile, dob, gender, marital_status,
            husband_name, husband_employer, village, block, bank_account, ifsc,
            doc_path, status, submitted_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)
           RETURNING id""",
        (app_no, name, mobile, dob, gender, marital, husband_name,
         husband_employer, village, block, bank_account, ifsc, doc_path,
         datetime.now()))
    new_id = cur.fetchone()[0]

    # status portal account; password is DOB as DDMMYYYY per dept. circular
    # Keep the historical initial credential for compatibility, but hash it
    # with a slow password KDF; the first login upgrades legacy SHA-256 rows.
    portal_pass = dob.strftime("%d%m%Y")
    cur.execute("SELECT count(*) FROM portal_users WHERE mobile = %s", (mobile,))
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES (%s,%s)",
                    (mobile, generate_password_hash(portal_pass)))
    conn.commit()

    app.logger.info("generating acknowledgment pdf application=%d" % new_id)
    pdf_bytes = generate_acknowledgment(cur, new_id)
    ack_dir = os.path.join(UPLOAD_DIR, "ack")
    os.makedirs(ack_dir, exist_ok=True)
    with open(os.path.join(ack_dir, "%d.pdf" % new_id), "wb") as out:
        out.write(pdf_bytes)

    cur.close(); conn.close()
    session.pop("form_data", None)
    session.pop("doc_path", None)
    session.pop("verified_mobile", None)
    send_sms(mobile, "Sewa Setu: application %s received. Track at the status portal "
                     "with mobile no. and password (DOB as DDMMYYYY)." % app_no)
    return render_template("confirmation.html", app_no=app_no, app_id=new_id)


def generate_acknowledgment(cur, app_id):
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, submitted_at, status FROM applications WHERE id = %s",
                (app_id,))
    row = cur.fetchone()
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "GOVERNMENT OF PURVANCHAL", ln=1, align="C")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, "Department of Social Welfare", ln=1, align="C")
    pdf.cell(0, 8, "Old Age Pension Scheme - Acknowledgment", ln=1, align="C")
    pdf.ln(4)
    # A single vector rectangle replaces thousands of tiny PDF line segments.
    pdf.rect(8, 10, 194, 277)
    labels = ["Application No", "Applicant Name", "Mobile", "Date of Birth",
              "Village", "Block", "Bank Account", "IFSC", "Submitted At", "Status"]
    pdf.set_font("Helvetica", "", 10)
    for label, val in zip(labels, row):
        try:
            pdf.cell(60, 8, label, border=1)
            pdf.cell(0, 8, str(val), border=1, ln=1)
        except Exception:
            pdf.cell(0, 8, "?", border=1, ln=1)
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, "This is a computer generated acknowledgment. Processing SLA "
                         "as per the Purvanchal Right to Public Services Act applies.")
    return bytes(pdf.output())


# ---------------------------------------------------------------------------
# Status portal (citizen login)
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET", "POST"])
def status_login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        valid, upgraded_hash = verify_portal_password(row[0], password) if row else (False, None)
        if valid:
            if upgraded_hash:
                upgrade_conn = get_db()
                upgrade_cur = upgrade_conn.cursor()
                upgrade_cur.execute("UPDATE portal_users SET password_hash = %s WHERE mobile = %s",
                                    (upgraded_hash, mobile))
                upgrade_conn.commit()
                upgrade_cur.close(); upgrade_conn.close()
            session.clear()
            session["citizen_authenticated"] = True
            session["portal_mobile"] = mobile
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM applications WHERE mobile = %s "
                        "ORDER BY submitted_at DESC LIMIT 1", (mobile,))
            r = cur.fetchone()
            cur.close(); conn.close()
            if r:
                return redirect(url_for("view_application", app_id=r[0]))
            flash("No application found for this mobile number.")
            return redirect(url_for("status_login"))
        flash("Something went wrong. Please try again.")
    return render_template("status_login.html")


@app.route("/application/<int:app_id>")
def view_application(app_id):
    if not session.get("citizen_authenticated"):
        flash("Please login to view application status.")
        return redirect(url_for("status_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, status, submitted_at, decided_at "
                "FROM applications WHERE id = %s AND mobile = %s",
                (app_id, session.get("portal_mobile")))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        abort(404)
    return render_template("application.html", a=row, app_id=app_id)


@app.route("/ack/<int:app_id>.pdf")
def ack_pdf(app_id):
    if not session.get("admin_authenticated"):
        if not session.get("citizen_authenticated"):
            abort(403)
        check_conn = get_db()
        check_cur = check_conn.cursor()
        check_cur.execute("SELECT 1 FROM applications WHERE id = %s AND mobile = %s",
                          (app_id, session.get("portal_mobile")))
        owned = check_cur.fetchone()
        check_cur.close(); check_conn.close()
        if not owned:
            abort(404)
    path = os.path.join(UPLOAD_DIR, "ack", "%d.pdf" % app_id)
    if not os.path.exists(path):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM applications WHERE id = %s", (app_id,))
        if not cur.fetchone():
            cur.close(); conn.close()
            abort(404)
        data = generate_acknowledgment(cur, app_id)
        cur.close(); conn.close()
        return send_file(io.BytesIO(data), mimetype="application/pdf")
    return send_file(path, mimetype="application/pdf")


@app.route("/status/reset", methods=["GET", "POST"])
def reset_password():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        otp = request.form.get("otp", "").strip()
        newpass = request.form.get("password", "")
        if not (len(mobile) == 10 and mobile.isdigit()):
            flash("Enter the registered 10-digit mobile number.")
            return render_template("reset.html")
        if not otp:
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT 1 FROM portal_users WHERE mobile = %s", (mobile,))
            exists = cur.fetchone()
            if exists:
                cur.execute("SELECT created_at FROM otps WHERE mobile = %s ORDER BY id DESC LIMIT 1",
                            (mobile,))
                previous = cur.fetchone()
                if previous and (datetime.now() - previous[0]).total_seconds() < 60:
                    flash("Please wait one minute before requesting another reset code.")
                else:
                    code = "%06d" % secrets.randbelow(1000000)
                    code_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
                    cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES (%s,%s,%s)",
                                (mobile, code_hash, datetime.now()))
                    conn.commit()
                    if send_sms(mobile, "Sewa Setu password reset OTP: %s. Valid for 5 minutes." % code):
                        session["reset_mobile"] = mobile
                    else:
                        cur.execute("DELETE FROM otps WHERE mobile = %s", (mobile,))
                        conn.commit()
            cur.close(); conn.close()
            flash("If the mobile number is registered, a reset OTP has been sent.")
            return render_template("reset.html", otp_sent=bool(session.get("reset_mobile")))
        reset_attempts = int(session.get("reset_otp_attempts", 0))
        if reset_attempts >= 5:
            session.pop("reset_mobile", None)
            session.pop("reset_otp_attempts", None)
            flash("Too many incorrect reset codes. Request a new code.")
            return render_template("reset.html")
        if session.get("reset_mobile") != mobile or len(otp) != 6 or not otp.isdigit():
            flash("Request a new password-reset OTP.")
            return render_template("reset.html")
        if len(newpass) < 10:
            flash("Choose a password with at least 10 characters.")
            return render_template("reset.html", otp_sent=True)
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT code, created_at FROM otps WHERE mobile = %s ORDER BY id DESC LIMIT 1",
                    (mobile,))
        row = cur.fetchone()
        otp_hash = hashlib.sha256(otp.encode("ascii")).hexdigest()
        if row and hmac.compare_digest(str(row[0]), otp_hash) and \
                (datetime.now() - row[1]).total_seconds() <= OTP_VALIDITY_SECONDS:
            cur.execute("UPDATE portal_users SET password_hash = %s WHERE mobile = %s",
                        (generate_password_hash(newpass), mobile))
            cur.execute("DELETE FROM otps WHERE mobile = %s", (mobile,))
            conn.commit()
            session.pop("reset_mobile", None)
            session.pop("reset_otp_attempts", None)
            flash("Your password has been updated. Please sign in.")
        else:
            session["reset_otp_attempts"] = reset_attempts + 1
            flash("The OTP is invalid or expired. Please request another one.")
        cur.close(); conn.close()
    return render_template("reset.html", otp_sent=bool(session.get("reset_mobile")))


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if (hmac.compare_digest(request.form.get("username", ""), ADMIN_USERNAME) and
                hmac.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD)):
            session.clear()
            session["admin_authenticated"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials.")
    return render_template("admin_login.html")


@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("admin_authenticated"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
    by_status = cur.fetchall()
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' "
                "AND submitted_at < %s", (datetime.now() - timedelta(days=SLA_DAYS),))
    overdue = cur.fetchone()[0]
    cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' "
                "GROUP BY block ORDER BY count(*) DESC")
    by_block = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_dashboard.html", by_status=by_status,
                           overdue=overdue, by_block=by_block, sla=SLA_DAYS)


@app.route("/admin/applications")
def admin_list():
    if not session.get("admin_authenticated"):
        return redirect(url_for("admin_login"))
    status = request.args.get("status", "PENDING")
    if status not in ("PENDING", "APPROVED", "REJECTED", "DEEMED_APPROVED"):
        abort(400)
    try:
        page = max(1, min(int(request.args.get("page", 1)), 10000))
    except (TypeError, ValueError):
        abort(400)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                "submitted_at FROM applications WHERE status = %s "
                "ORDER BY submitted_at ASC LIMIT 50 OFFSET %s",
                (status, (page - 1) * 50))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_list.html", rows=rows, status=status, page=page)


@app.route("/admin/application/<int:app_id>")
def admin_view(app_id):
    if not session.get("admin_authenticated"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, gender, "
                "marital_status, village, block, bank_account, ifsc, doc_path, "
                "status, submitted_at, decided_at, decided_by "
                "FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        abort(404)
    return render_template("admin_view.html", a=row, app_id=app_id)


@app.route("/admin/approve/<int:app_id>", methods=["POST"])
def admin_approve(app_id):
    if not session.get("admin_authenticated"):
        abort(403)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = 'APPROVED', decided_at = %s, decided_by = %s "
                "WHERE id = %s AND status = 'PENDING'",
                (datetime.now(), ADMIN_USERNAME, app_id))
    changed = cur.rowcount
    conn.commit()
    cur.close(); conn.close()
    flash("Application approved." if changed else "Only pending applications can be approved.")
    return redirect(url_for("admin_list"))


@app.route("/admin/reject/<int:app_id>", methods=["POST"])
def admin_reject(app_id):
    if not session.get("admin_authenticated"):
        abort(403)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = 'REJECTED', decided_at = %s, decided_by = %s "
                "WHERE id = %s AND status = 'PENDING'",
                (datetime.now(), ADMIN_USERNAME, app_id))
    changed = cur.rowcount
    conn.commit()
    cur.close(); conn.close()
    flash("Application rejected." if changed else "Only pending applications can be rejected.")
    return redirect(url_for("admin_list"))


# ---------------------------------------------------------------------------
# Unused / legacy
# ---------------------------------------------------------------------------

def export_to_excel(rows):
    # Was used for the monthly disbursement report before eKosh integration.
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["application_no", "name", "account", "ifsc", "amount"])
    for r in rows:
        writer.writerow(list(r) + ["250"])
    return out.getvalue()


def old_payment_gateway_callback(txn):
    # retained for reference; eChallan integration was descoped in Phase 2
    status = txn.get("STATUS")
    if status == "0300":
        return "SUCCESS"
    elif status == "0399":
        return "FAILED"
    return "PENDING"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
