"""Account-scoped, signed, durable Kotak authentication (no browser cookies)."""
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import time
from urllib.parse import urlparse
from threading import Thread

import httpx
import pyotp
from sqlalchemy import Column, Float, Integer, MetaData, String, Table, Text, insert, select, update
from sqlalchemy.exc import IntegrityError

metadata = MetaData()
operations = Table("kotak_automation_operations", metadata,
    Column("id", String(96), primary_key=True), Column("username", String(80), nullable=False),
    Column("day", String(10), nullable=False), Column("state", String(32), nullable=False),
    Column("attempts", Integer, nullable=False), Column("updated", Float, nullable=False),
    Column("details", Text, nullable=False))
nonces = Table("kotak_automation_nonces", metadata,
    Column("nonce", String(64), primary_key=True), Column("created", Float, nullable=False))

records = Table("kotak_automation_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("operation_id", String(96), nullable=False), Column("created", Float, nullable=False),
    Column("attempt", Integer, nullable=False), Column("state", String(32), nullable=False),
    Column("details", Text, nullable=False))


def source_identity():
    root = Path(__file__).resolve().parents[1]
    files = ("services/kotak_automation.py", "blueprints/kotak_automation.py",
             "broker/kotak/api/auth_api.py", "utils/auth_utils.py", "blueprints/brlogin.py",
             "database/auth_db.py", "utils/session.py", "utils/config.py", "utils/env_check.py", "app.py")
    return {"contract_version": 1, "automation_source_digest": hashlib.sha256(
        b"".join(name.encode() + (root / name).read_bytes() for name in files)).hexdigest()}


def database():
    from database.auth_db import engine
    metadata.create_all(engine)
    if engine.dialect.name == "sqlite":
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text("CREATE TRIGGER IF NOT EXISTS kotak_automation_events_no_update BEFORE UPDATE ON kotak_automation_events BEGIN SELECT RAISE(ABORT, 'authentication events are immutable'); END"))
            conn.execute(text("CREATE TRIGGER IF NOT EXISTS kotak_automation_events_no_delete BEFORE DELETE ON kotak_automation_events BEGIN SELECT RAISE(ABORT, 'authentication events are immutable'); END"))
    return engine


def configuration():
    if os.getenv("KOTAK_AUTOLOGIN_ENABLED", "true").lower() != "true":
        raise ValueError("Kotak automation is disabled")
    names = ("KOTAK_AUTOLOGIN_USERNAME", "KOTAK_MOBILE_NUMBER", "KOTAK_MPIN",
             "KOTAK_TOTP_SECRET", "OPENALGO_AUTOMATION_SECRET")
    values = {name: os.getenv(name, "").strip() for name in names}
    if any(not value for value in values.values()):
        raise ValueError("Kotak automation settings are incomplete")
    if len(values["OPENALGO_AUTOMATION_SECRET"].encode()) < 32:
        raise ValueError("Automation secret must contain at least 32 bytes")
    if not re.fullmatch(r"\+?\d{10,12}", values["KOTAK_MOBILE_NUMBER"]):
        raise ValueError("Invalid Kotak mobile number format")
    if not re.fullmatch(r"\d{6}", values["KOTAK_MPIN"]):
        raise ValueError("Kotak MPIN must contain six digits")
    try:
        base64.b32decode(values["KOTAK_TOTP_SECRET"].upper(), casefold=True)
        pyotp.TOTP(values["KOTAK_TOTP_SECRET"]).now()
    except Exception as exc:
        raise ValueError("Invalid Kotak authenticator seed format") from exc
    from database.user_db import find_user_by_exact_username
    if not find_user_by_exact_username(values["KOTAK_AUTOLOGIN_USERNAME"]):
        raise ValueError("Configured OpenAlgo user does not exist")
    from utils.config import get_broker_api_key, get_broker_api_secret
    if not get_broker_api_key() or not get_broker_api_secret():
        raise ValueError("Kotak UCC and API access token must be configured")
    return values


def signature(secret, method, path, stamp, nonce, body, api_key=""):
    canonical = "\n".join((method, path, stamp, nonce, hashlib.sha256(body).hexdigest(), hashlib.sha256(api_key.encode()).hexdigest()))
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def authorize(request):
    config = configuration()
    # The public website Host is never a valid automation target, even with a signed request.
    if request.host.split(":", 1)[0] not in {"openalgo-automation", "localhost", "127.0.0.1"}:
        raise PermissionError("Automation requires the private service hostname")
    stamp = request.headers.get("X-Automation-Time", "")
    nonce = request.headers.get("X-Automation-Nonce", "")
    try:
        valid_time = abs(time.time() - float(stamp)) <= 30
    except ValueError:
        valid_time = False
    if not valid_time or not re.fullmatch(r"[a-f0-9]{32}", nonce):
        raise PermissionError("Invalid automation request")
    expected = signature(config["OPENALGO_AUTOMATION_SECRET"], request.method, request.path,
                         stamp, nonce, request.get_data(), request.headers.get("X-OpenAlgo-Api-Key", ""))
    if not hmac.compare_digest(expected, request.headers.get("X-Automation-Signature", "")):
        raise PermissionError("Invalid automation signature")
    from database.auth_db import get_username_by_apikey
    if get_username_by_apikey(request.headers.get("X-OpenAlgo-Api-Key", "")) != config["KOTAK_AUTOLOGIN_USERNAME"]:
        raise PermissionError("Automation API key does not match the configured account")
    try:
        with database().begin() as conn:
            conn.execute(insert(nonces).values(nonce=nonce, created=time.time()))
    except IntegrityError as exc:
        raise PermissionError("Replayed automation request") from exc
    return config


@contextmanager
def authentication_lock(username):
    # A shared-volume flock survives worker/thread concurrency without an expiring lease.
    from database.auth_db import engine
    if engine.dialect.name != "sqlite":
        raise ValueError("Kotak automation currently requires the deployed SQLite configuration")
    directory = Path(engine.url.database).resolve().parent
    with (directory / ("kotak-auth-" + hashlib.sha256(username.encode()).hexdigest() + ".lock")).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Broker authentication already in progress") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def probe_token(token):
    """Read the real broker, regardless of Analyzer mode; never infer expiry from connectivity."""
    if not token:
        return "expired"
    parts = token.split(":::")
    if len(parts) < 4 or not parts[2].startswith("https://"):
        return "unknown"
    target = urlparse(parts[2])
    if not target.hostname or not target.hostname.endswith(".kotaksecurities.com") or target.username:
        return "unknown"
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(parts[2] + "/quick/user/limits",
                headers={"Sid": parts[1], "Auth": parts[0], "neo-fin-key": "neotradeapi",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"jData": '{"seg":"ALL","exch":"ALL","prod":"ALL"}'})
        if response.status_code == 401:
            return "expired"
        data = response.json()
        if not isinstance(data, dict):
            return "unknown"
        if response.is_success and data.get("stat") == "Ok":
            return "valid"
        message = str(data.get("emsg", "")).lower()
        if any(term in message for term in ("session expired", "invalid session", "token expired", "invalid token")):
            return "expired"
    except (httpx.HTTPError, ValueError):
        pass
    return "unknown"


def status(config):
    from database.auth_db import get_auth_token_fresh
    from database.master_contract_status_db import get_status
    from utils.auth_utils import should_download_master_contract
    from utils.session import get_trading_session_date, has_login_this_trading_session
    from database.settings_db import get_analyze_mode
    username = config["KOTAK_AUTOLOGIN_USERNAME"]
    token = get_auth_token_fresh(username)
    authentication = probe_token(token)
    if authentication == "valid" and not has_login_this_trading_session(username):
        authentication = "expired"
    contract = get_status("kotak")
    download, _ = should_download_master_contract("kotak")
    with database().connect() as conn:
        rows = conn.execute(select(operations).where(operations.c.username == username,
            operations.c.day == get_trading_session_date()).order_by(operations.c.updated.desc()).limit(10)).mappings().all()
    output = []
    for row in rows:
        if authentication == "valid" and row["state"] in {"unresolved", "credential_rejected"}:
            finish(row["id"], "resolved", row["attempts"], "Current broker authentication confirmed; original failure retained in events")
        with database().connect() as conn:
            events = conn.execute(select(records).where(records.c.operation_id == row["id"]).order_by(records.c.id)).mappings().all()
            latest = conn.execute(select(operations).where(operations.c.id == row["id"])).mappings().one()
        output.append(dict(latest) | {"details": json.loads(latest["details"]),
            "events": [dict(event) | {"details": json.loads(event["details"])} for event in events]})
    return {**source_identity(), "authentication": authentication, "mode": "analyze" if get_analyze_mode() else "live",
        "contracts_ready": bool(contract.get("is_ready") and not download),
        "contracts_status": contract.get("status", "unknown"), "operations": output}


def finish(operation, state, attempts, reason):
    with database().begin() as conn:
        conn.execute(update(operations).where(operations.c.id == operation).values(
            state=state, attempts=attempts, updated=time.time(), details=json.dumps({"reason": reason})))
        conn.execute(insert(records).values(operation_id=operation, created=time.time(), attempt=attempts,
            state=state, details=json.dumps({"reason": reason})))


def clock_verified():
    # An HTTPS broker Date header is an independent clock check, not a price timestamp.
    try:
        begin = time.time()
        with httpx.Client(timeout=5) as client:
            response = client.head("https://mis.kotaksecurities.com/login/1.0/tradeApiLogin")
        end = time.time()
        remote = parsedate_to_datetime(response.headers["Date"]).timestamp()
        return end - begin <= 5 and abs(remote - (begin + end) / 2) <= 5
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        return False


def worker(app, config, operation):
    attempts = 0
    try:
        with app.app_context(), authentication_lock(config["KOTAK_AUTOLOGIN_USERNAME"]):
            current = status(config)
            if current["mode"] != "analyze":
                finish(operation, "blocked", 0, "Analyzer mode is required")
                return
            if current["authentication"] == "valid":
                finish(operation, "completed", 0, "Existing broker authentication reused")
                return
            if current["authentication"] != "expired":
                finish(operation, "blocked", 0, "Broker authentication validity is unknown")
                return
            if not clock_verified():
                finish(operation, "blocked", 0, "Server clock could not be verified against Kotak")
                return
            from broker.kotak.api.auth_api import authenticate_broker
            from utils.auth_utils import persist_broker_authentication
            from database.auth_db import register_session, remove_session
            from utils.session import get_trading_session_date
            for attempts in range(1, 4):
                finish(operation, "running", attempts, "Authenticating broker")
                # Never submit a code with fewer than five seconds remaining.
                remainder = 30 - time.time() % 30
                if remainder < 5:
                    time.sleep(remainder + .1)
                token, error = authenticate_broker(config["KOTAK_MOBILE_NUMBER"],
                    pyotp.TOTP(config["KOTAK_TOTP_SECRET"]).now(), config["KOTAK_MPIN"])
                if token:
                    machine_session = "automation:" + hashlib.sha256(config["KOTAK_AUTOLOGIN_USERNAME"].encode()).hexdigest()[:16] + ":" + get_trading_session_date()
                    remove_session(machine_session)
                    if not register_session(config["KOTAK_AUTOLOGIN_USERNAME"], machine_session,
                            device_info="Kotak unattended authentication", broker="kotak"):
                        finish(operation, "unresolved", attempts, "Machine authentication identity could not be persisted")
                        return
                    if not persist_broker_authentication(token, config["KOTAK_AUTOLOGIN_USERNAME"], "kotak"):
                        remove_session(machine_session)
                        finish(operation, "unresolved", attempts, "Broker token storage could not be confirmed")
                        return
                    if probe_token(token) != "valid":
                        finish(operation, "unresolved", attempts, "New broker authentication could not be confirmed")
                        return
                    finish(operation, "completed", attempts, "Broker authentication renewed")
                    return
                if error and ("temporarily unavailable" in error or "rate limited" in error):
                    finish(operation, "retrying", attempts, "Explicit transient broker failure; bounded retry pending")
                    if attempts < 3:
                        time.sleep(30)
                        continue
                    finish(operation, "blocked", attempts, "Broker authentication retry allowance exhausted")
                elif error and "rejected" in error:
                    finish(operation, "credential_rejected", attempts, "Kotak rejected authentication; check credentials privately")
                else:
                    finish(operation, "unresolved", attempts, "Authentication result is ambiguous; automatic retry withheld")
                return
    except Exception:
        # Never persist exception text: broker/client exceptions can contain credentials.
        finish(operation, "unresolved", attempts, "Authentication interrupted or unavailable; inspect broker state")


def ensure(app, config, operation):
    from utils.session import get_trading_session_date
    if not isinstance(operation, str) or not re.fullmatch(r"[a-zA-Z0-9:_-]{1,96}", operation):
        raise ValueError("A valid operation_id is required")
    username = config["KOTAK_AUTOLOGIN_USERNAME"]
    with authentication_lock(username):
        current = status(config)
        matching = next((row for row in current["operations"] if row["id"] == operation), None)
        if matching:
            if matching["state"] == "running" and current["authentication"] == "valid":
                finish(operation, "completed", matching["attempts"], "Authentication confirmed after interrupted acknowledgement")
            elif matching["state"] == "running" and time.time() - matching["updated"] > 180:
                finish(operation, "unresolved", matching["attempts"], "Interrupted authentication requires broker reconciliation")
            return status(config)
        if any(row["state"] in {"running", "retrying", "unresolved", "credential_rejected"} for row in current["operations"]):
            raise RuntimeError("Previous authentication requires reconciliation; no new login was submitted")
        with database().begin() as conn:
            conn.execute(insert(operations).values(id=operation, username=username,
                day=get_trading_session_date(), state="running", attempts=0, updated=time.time(), details="{}"))
    Thread(target=worker, args=(app, config, operation), daemon=True, name="kotak-auto-auth").start()
    return {"operation_id": operation, "state": "running"}
