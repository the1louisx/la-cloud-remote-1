"""
LA Software Cloud Remote - Backend Server
A simple API for remote ARM/DISARM control of the Mac laptop alarm.

Security hardening applied — see inline comments marked [HARDENED].

Auth model (after pairing refactor):
  Mac app  ←→  server  : authenticated via device_id + device_token
  Phone    ←→  server  : authenticated via session_token (obtained by pairing)
  QR code  :  contains only a short-lived pairing_token — no stable IDs.
"""

from contextlib import asynccontextmanager
import asyncio
import logging
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
import secrets
import time
from typing import Optional
import json
from pathlib import Path


# ---------------------------------------------------------------------------
# Logging  [HARDENED]
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("la-remote")


# ---------------------------------------------------------------------------
# Configuration  [HARDENED] — all limits in one place
# ---------------------------------------------------------------------------
MAX_QUEUE_SIZE = 5
DEVICE_EXPIRY_SECONDS = 86400          # 24 hours
MAX_DEVICES = 200                       # cap total registered devices
MAX_LOG_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB events.log cap
MAX_REQUEST_BODY_BYTES = 10_000        # 10 KB max request body

# Escalating PIN lockout schedule
LOCKOUT_BURST_SIZE = 5
LOCKOUT_SCHEDULE_SECONDS = (60, 300, 900, 1800, 3600)  # 1m → 60m
LOCKOUT_DECAY_SECONDS = 86400                           # 24h quiet → −1 level

# Pairing tokens  [HARDENED-PAIRING]
PAIRING_TOKEN_TTL_SECONDS = 120        # 2 minutes — QR is only valid this long
MAX_ACTIVE_PAIRING_TOKENS = 500        # global cap to bound memory

# Phone sessions  [HARDENED-PAIRING]
SESSION_TTL_SECONDS = 86400            # 24 hours
MAX_ACTIVE_SESSIONS = 1000             # global cap

# CORS
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

EVENTS_LOG_PATH = Path("events.log")


# ---------------------------------------------------------------------------
# Rate limiter  [HARDENED]
# ---------------------------------------------------------------------------
def _get_real_ip(request: Request) -> str:
    """Return the real client IP, respecting Cloudflare/Render proxy headers."""
    return (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.client.host
    )


limiter = Limiter(key_func=_get_real_ip)


# ---------------------------------------------------------------------------
# Request body size middleware  [HARDENED]
# ---------------------------------------------------------------------------
class LimitRequestSizeMiddleware(BaseHTTPMiddleware):
    """Reject request bodies larger than MAX_REQUEST_BODY_BYTES."""

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_REQUEST_BODY_BYTES:
            return Response("Request body too large", status_code=413)
        return await call_next(request)


# ---------------------------------------------------------------------------
# Periodic cleanup  [HARDENED]
# ---------------------------------------------------------------------------
async def _periodic_cleanup():
    """Hourly cleanup of expired devices, pairing tokens, and sessions."""
    while True:
        cleanup_old_devices()
        cleanup_expired_pairing_tokens()
        cleanup_expired_sessions()
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_periodic_cleanup())
    logger.info("Server started — periodic cleanup scheduled")
    yield
    task.cancel()


# ---------------------------------------------------------------------------
# App init
# ---------------------------------------------------------------------------
app = FastAPI(title="LA Software Cloud Remote", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(LimitRequestSizeMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type"],
)


# ===========================================================================
# IN-MEMORY STORES  (each has get/set/delete helpers for Redis swap later)
# ===========================================================================

# --- 1. Devices (Mac registrations) ---
# device_id -> { pin_hash, device_token, queue[], last_seen }
devices: dict = {}

# --- 2. Pairing tokens (short-lived, QR payload) ---
# pairing_token -> { device_id, expires_at }
pairing_tokens: dict = {}

# --- 3. Phone sessions (post-pairing auth for /command) ---
# session_token -> { device_id, expires_at }
sessions: dict = {}

# --- 4. PIN lockout state ---
# device_id -> { consecutive_failures, level, locked_until, last_failure_at }
pin_state: dict = {}


# ===========================================================================
# STORAGE HELPERS — swap these for Redis later, nothing else changes
# ===========================================================================

# -- Pairing tokens --

def _set_pairing_token(token: str, device_id: str, ttl: int) -> None:
    pairing_tokens[token] = {
        "device_id": device_id,
        "expires_at": time.time() + ttl,
    }


def _get_pairing_token(token: str) -> Optional[dict]:
    entry = pairing_tokens.get(token)
    if entry and time.time() < entry["expires_at"]:
        return entry
    # Expired — remove lazily
    pairing_tokens.pop(token, None)
    return None


def _delete_pairing_token(token: str) -> None:
    pairing_tokens.pop(token, None)


def cleanup_expired_pairing_tokens() -> None:
    now = time.time()
    expired = [t for t, v in pairing_tokens.items() if now >= v["expires_at"]]
    for t in expired:
        del pairing_tokens[t]
    if expired:
        logger.info("Cleaned up %d expired pairing token(s)", len(expired))


# -- Sessions --

def _set_session(token: str, device_id: str, ttl: int) -> None:
    sessions[token] = {
        "device_id": device_id,
        "expires_at": time.time() + ttl,
    }


def _get_session(token: str) -> Optional[dict]:
    entry = sessions.get(token)
    if entry and time.time() < entry["expires_at"]:
        return entry
    sessions.pop(token, None)
    return None


def _delete_session(token: str) -> None:
    sessions.pop(token, None)


def _delete_sessions_for_device(device_id: str) -> None:
    """Revoke all phone sessions for a device (e.g. on re-pair)."""
    to_del = [t for t, v in sessions.items() if v["device_id"] == device_id]
    for t in to_del:
        del sessions[t]


def cleanup_expired_sessions() -> None:
    now = time.time()
    expired = [t for t, v in sessions.items() if now >= v["expires_at"]]
    for t in expired:
        del sessions[t]
    if expired:
        logger.info("Cleaned up %d expired session(s)", len(expired))


# -- PIN lockout state --

def _new_pin_state() -> dict:
    return {"consecutive_failures": 0, "level": 0, "locked_until": 0.0, "last_failure_at": 0.0}

def _get_pin_state(device_id: str) -> dict:
    return pin_state.get(device_id, _new_pin_state())

def _set_pin_state(device_id: str, state: dict) -> None:
    pin_state[device_id] = state

def _delete_pin_state(device_id: str) -> None:
    pin_state.pop(device_id, None)


# ===========================================================================
# HELPER FUNCTIONS
# ===========================================================================

def generate_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def cleanup_old_devices():
    current_time = time.time()
    expired_ids = [
        did for did, data in devices.items()
        if current_time - data.get("last_seen", 0) > DEVICE_EXPIRY_SECONDS
    ]
    for did in expired_ids:
        del devices[did]
        _delete_pin_state(did)
        _delete_sessions_for_device(did)
    if expired_ids:
        logger.info("Cleaned up %d expired device(s)", len(expired_ids))


def append_usage_event(e: "UsageEvent") -> None:
    record = {
        "user_id": e.user_id,
        "event": e.event,
        "timestamp": e.timestamp,
        "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    line = json.dumps(record, ensure_ascii=False)
    with EVENTS_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# Escalating PIN lockout helpers  [HARDENED]
# ---------------------------------------------------------------------------

def _apply_decay(state: dict, now: float) -> dict:
    if state["level"] > 0 and state["last_failure_at"] > 0:
        quiet_seconds = now - state["last_failure_at"]
        levels_to_drop = int(quiet_seconds // LOCKOUT_DECAY_SECONDS)
        if levels_to_drop > 0:
            state["level"] = max(0, state["level"] - levels_to_drop)
            logger.info("Lockout level decayed by %d → now level %d", levels_to_drop, state["level"])
    return state


def check_pin_lockout(device_id: str) -> None:
    now = time.time()
    state = _get_pin_state(device_id)
    state = _apply_decay(state, now)
    _set_pin_state(device_id, state)
    if now < state["locked_until"]:
        remaining = int(state["locked_until"] - now)
        logger.warning(
            "PIN lockout active for device %s… (level %d, %ds left)",
            device_id[:8], state["level"], remaining,
        )
        raise HTTPException(status_code=429, detail=f"Too many attempts. Retry in {remaining}s.")


def record_pin_failure(device_id: str) -> None:
    now = time.time()
    state = _get_pin_state(device_id)
    state = _apply_decay(state, now)
    state["consecutive_failures"] += 1
    state["last_failure_at"] = now
    if state["consecutive_failures"] >= LOCKOUT_BURST_SIZE:
        max_level = len(LOCKOUT_SCHEDULE_SECONDS) - 1
        lock_seconds = LOCKOUT_SCHEDULE_SECONDS[min(state["level"], max_level)]
        state["locked_until"] = now + lock_seconds
        state["consecutive_failures"] = 0
        state["level"] = min(state["level"] + 1, max_level)
        logger.warning(
            "PIN lockout TRIGGERED for device %s… (level %d, %ds)",
            device_id[:8], state["level"], lock_seconds,
        )
    _set_pin_state(device_id, state)


def record_pin_success(device_id: str) -> None:
    state = _get_pin_state(device_id)
    if state["consecutive_failures"] > 0:
        state["consecutive_failures"] = 0
        _set_pin_state(device_id, state)


# ---------------------------------------------------------------------------
# Device-token verification helper (used by laptop-only endpoints)
# ---------------------------------------------------------------------------

def _require_device_auth(device_id: str, device_token: str) -> dict:
    """Validate device_id + device_token.  Returns the device dict.
    Uniform 403 — does not reveal whether device_id exists."""
    _FAIL = HTTPException(status_code=403, detail="Invalid credentials")
    device = devices.get(device_id)
    if device is None:
        raise _FAIL
    if not secrets.compare_digest(device["device_token"], device_token):
        raise _FAIL
    device["last_seen"] = time.time()
    return device


# ===========================================================================
# REQUEST / RESPONSE MODELS
# ===========================================================================

# -- Registration (laptop → server) --
class RegisterRequest(BaseModel):
    device_id: str = Field(..., min_length=8, max_length=64)
    pin_hash: str = Field(..., min_length=64, max_length=64)

class RegisterResponse(BaseModel):
    device_token: str

# -- Pairing --
class PairingCreateRequest(BaseModel):
    device_id: str = Field(..., min_length=8, max_length=64)
    device_token: str = Field(..., min_length=1, max_length=128)

class PairingCreateResponse(BaseModel):
    pairing_token: str
    expires_in: int

class PairingConsumeRequest(BaseModel):
    pairing_token: str = Field(..., min_length=1, max_length=128)
    pin_hash: str = Field(..., min_length=64, max_length=64)

class PairingConsumeResponse(BaseModel):
    session_token: str

# -- Command (phone → server, session-authed) --
class CommandRequest(BaseModel):
    session_token: str = Field(..., min_length=1, max_length=128)
    command: str = Field(..., max_length=10)

class CommandResponse(BaseModel):
    status: str

# -- Poll (laptop → server) --
class PollRequest(BaseModel):
    device_id: str = Field(..., min_length=8, max_length=64)
    device_token: str = Field(..., min_length=1, max_length=128)

class PollResponse(BaseModel):
    command: Optional[str]

# -- Health --
class HealthResponse(BaseModel):
    status: str
    message: str

# -- Update-session (laptop → server, post-DISARM session rotation) --
class UpdateSessionRequest(BaseModel):
    device_id: str = Field(..., min_length=8, max_length=64)
    device_token: str = Field(..., min_length=1, max_length=128)

class UpdateSessionResponse(BaseModel):
    status: str

# -- Events (laptop → server) --
class UsageEvent(BaseModel):
    device_id: str = Field(..., min_length=8, max_length=64)
    device_token: str = Field(..., min_length=1, max_length=128)
    event: str = Field(..., max_length=20)
    timestamp: str = Field(..., max_length=30)

class UsageEventResponse(BaseModel):
    ok: bool


# ===========================================================================
# ENDPOINTS
# ===========================================================================

# ---- Health ----

@app.get("/", response_model=HealthResponse)
@limiter.limit("60/minute")
async def health_check(request: Request):
    return {"status": "ok", "message": "LA server running"}


# ---- Registration (laptop only) ----

@app.post("/register", response_model=RegisterResponse)
@limiter.limit("5/minute")
async def register(request: Request, body: RegisterRequest):
    """
    Register a Mac device.
    Called by the Mac app on first setup or re-register after server restart.
    """
    cleanup_old_devices()

    # Prevent hijack — existing device requires matching pin_hash
    if body.device_id in devices:
        existing = devices[body.device_id]
        if not secrets.compare_digest(existing["pin_hash"], body.pin_hash):
            logger.warning("Register rejected — PIN mismatch for device %s…", body.device_id[:8])
            raise HTTPException(status_code=403, detail="Device already registered with a different PIN")

    # Cap total devices
    if body.device_id not in devices and len(devices) >= MAX_DEVICES:
        logger.warning("Device cap reached (%d/%d)", len(devices), MAX_DEVICES)
        raise HTTPException(status_code=503, detail="Server at capacity. Try again later.")

    device_token = generate_token(32)

    devices[body.device_id] = {
        "pin_hash": body.pin_hash,
        "device_token": device_token,
        "queue": [],
        "last_seen": time.time(),
    }

    logger.info("Device registered: %s…", body.device_id[:8])
    return {"device_token": device_token}


# ---- Pairing (new QR flow) ----

@app.post("/pairing/create", response_model=PairingCreateResponse)
@limiter.limit("10/minute")
async def pairing_create(request: Request, body: PairingCreateRequest):
    """
    Laptop requests a short-lived pairing token to embed in a QR code.
    Auth: device_id + device_token (only the laptop can call this).
    The QR contains ONLY the pairing_token — no device_id.
    """
    _require_device_auth(body.device_id, body.device_token)

    # Cap active pairing tokens to bound memory
    cleanup_expired_pairing_tokens()
    if len(pairing_tokens) >= MAX_ACTIVE_PAIRING_TOKENS:
        raise HTTPException(status_code=503, detail="Too many active pairing tokens. Try again.")

    # Revoke any existing sessions for this device (re-pair = old phone loses access)
    _delete_sessions_for_device(body.device_id)

    token = generate_token(32)
    _set_pairing_token(token, body.device_id, PAIRING_TOKEN_TTL_SECONDS)

    logger.info("Pairing token created for device %s… (expires in %ds)", body.device_id[:8], PAIRING_TOKEN_TTL_SECONDS)
    return {"pairing_token": token, "expires_in": PAIRING_TOKEN_TTL_SECONDS}


@app.post("/pairing/consume", response_model=PairingConsumeResponse)
@limiter.limit("10/minute")
async def pairing_consume(request: Request, body: PairingConsumeRequest):
    """
    Phone scans QR, posts pairing_token + pin_hash.
    On success: returns a session_token the phone uses for /command.
    The pairing_token is deleted immediately (one-time use).

    Uniform 403 on all failures — does not reveal whether the token
    existed, expired, or the PIN was wrong.
    """
    _FAIL = HTTPException(status_code=403, detail="Invalid credentials")

    entry = _get_pairing_token(body.pairing_token)
    if entry is None:
        # Token doesn't exist or expired — still count a failure against a
        # synthetic key so rapid scanning of random tokens gets rate-limited.
        record_pin_failure("__pairing_global__")
        raise _FAIL

    device_id = entry["device_id"]

    # Check escalating lockout for this device
    check_pin_lockout(device_id)

    device = devices.get(device_id)
    if device is None:
        # Device vanished between create and consume (server restart, expiry)
        _delete_pairing_token(body.pairing_token)
        raise _FAIL

    # Verify PIN
    if not secrets.compare_digest(device["pin_hash"], body.pin_hash):
        record_pin_failure(device_id)
        # Do NOT delete the pairing token on wrong PIN — let the user retry
        # within the 120s window.  The escalating lockout protects against brute-force.
        raise _FAIL

    record_pin_success(device_id)

    # --- Success: issue session, burn pairing token ---
    _delete_pairing_token(body.pairing_token)

    # Revoke any old sessions for this device
    _delete_sessions_for_device(device_id)

    # Cap total sessions
    cleanup_expired_sessions()
    if len(sessions) >= MAX_ACTIVE_SESSIONS:
        raise HTTPException(status_code=503, detail="Too many active sessions.")

    session_token = generate_token(32)
    _set_session(session_token, device_id, SESSION_TTL_SECONDS)

    logger.info("Session created for device %s… via pairing", device_id[:8])
    return {"session_token": session_token}


# ---- Command (phone, session-authed — no PIN on every call) ----

@app.post("/command", response_model=CommandResponse)
@limiter.limit("20/minute")
async def command(request: Request, body: CommandRequest):
    """
    Queue a command for a device.
    Auth: session_token (obtained from /pairing/consume).
    No device_id or PIN needed — the session_token maps to a device.
    """
    _FAIL = HTTPException(status_code=403, detail="Invalid credentials")

    session = _get_session(body.session_token)
    if session is None:
        raise _FAIL

    device_id = session["device_id"]
    device = devices.get(device_id)
    if device is None:
        # Device expired / server restarted — session is orphaned
        _delete_session(body.session_token)
        raise _FAIL

    # Validate command
    if body.command not in ("ARM", "DISARM"):
        raise HTTPException(status_code=400, detail="Invalid command. Must be ARM or DISARM")

    # Queue size limit
    if len(device["queue"]) >= MAX_QUEUE_SIZE:
        raise HTTPException(status_code=429, detail="Too many pending commands. Please wait.")

    device["queue"].append(body.command)
    return {"status": "ok"}


# ---- Update-session (laptop, post-DISARM rotation) ----

@app.post("/update-session", response_model=UpdateSessionResponse)
@limiter.limit("10/minute")
async def update_session(request: Request, body: UpdateSessionRequest):
    """
    After DISARM, laptop calls this to revoke old phone sessions and force
    re-pairing.  Auth: device_id + device_token.
    """
    _require_device_auth(body.device_id, body.device_token)

    # Revoke all phone sessions for this device
    _delete_sessions_for_device(body.device_id)

    logger.info("Sessions revoked for device %s… (post-DISARM)", body.device_id[:8])
    return {"status": "ok"}


# ---- Poll (laptop) ----

@app.post("/poll", response_model=PollResponse)
@limiter.limit("120/minute")
async def poll(request: Request, body: PollRequest):
    """
    Poll for pending commands.  Called by the Mac app every second.
    Auth: device_id + device_token.
    """
    device = _require_device_auth(body.device_id, body.device_token)

    if device["queue"]:
        cmd = device["queue"].pop(0)
        return {"command": cmd}

    return {"command": None}


# ---- Events (laptop) ----

@app.post("/events", response_model=UsageEventResponse)
@limiter.limit("10/minute")
async def events(request: Request, body: UsageEvent):
    """
    Record a usage event for retention analytics.
    Auth: device_id + device_token (laptop only).
    """
    _require_device_auth(body.device_id, body.device_token)

    allowed_events = {"armed", "disarmed", "alarm_fired"}
    if body.event not in allowed_events:
        raise HTTPException(status_code=400, detail="Invalid event type")

    if EVENTS_LOG_PATH.exists() and EVENTS_LOG_PATH.stat().st_size > MAX_LOG_SIZE_BYTES:
        logger.warning("events.log size cap reached (%d bytes)", MAX_LOG_SIZE_BYTES)
        raise HTTPException(status_code=429, detail="Event log full")

    append_usage_event(body)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=10000)
