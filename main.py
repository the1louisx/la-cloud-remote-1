"""
LA Software Cloud Remote - Backend Server
A simple API for remote ARM/DISARM control of the Mac laptop alarm.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import secrets
import time
from typing import Optional
import json
from pathlib import Path

app = FastAPI(title="LA Software Cloud Remote")

# Enable CORS for the phone web page
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict to your domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory storage: device_id -> { "pin_hash": ..., "device_token": ..., "queue": [], "last_seen": ... }
devices: dict = {}

# Configuration
MAX_QUEUE_SIZE = 5
DEVICE_EXPIRY_SECONDS = 86400  # 24 hours

# File where we store anonymous usage events (for retention)
EVENTS_LOG_PATH = Path("events.log")


def generate_token(length: int = 32) -> str:
    """Generate a secure random token."""
    return secrets.token_urlsafe(length)


def cleanup_old_devices():
    """Remove devices that haven't been seen in 24 hours."""
    current_time = time.time()
    expired_ids = [
        device_id
        for device_id, data in devices.items()
        if current_time - data.get("last_seen", 0) > DEVICE_EXPIRY_SECONDS
    ]
    for device_id in expired_ids:
        del devices[device_id]


def append_usage_event(e: "UsageEvent") -> None:
    """Append one anonymous usage event as a JSON line."""
    record = {
        "user_id": e.user_id,
        "event": e.event,
        "timestamp": e.timestamp,
        "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    line = json.dumps(record, ensure_ascii=False)
    with EVENTS_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# --- Request/Response Models ---


class RegisterRequest(BaseModel):
    device_id: str  # Client-generated UUID
    pin_hash: str


class RegisterResponse(BaseModel):
    device_token: str


class CommandRequest(BaseModel):
    device_id: str
    pin_hash: str
    command: str


class CommandResponse(BaseModel):
    status: str


class PollRequest(BaseModel):
    device_id: str
    device_token: str


class PollResponse(BaseModel):
    command: Optional[str]


class HealthResponse(BaseModel):
    status: str
    message: str


# New: analytics event models
class UsageEvent(BaseModel):
    # On the Mac, you’ll send DeviceIdentity.deviceId here
    user_id: str       # anonymous per-device ID
    event: str         # "armed", "disarmed", "alarm_fired"
    timestamp: str     # ISO8601 string from the client (UTC)


class UsageEventResponse(BaseModel):
    ok: bool


# --- Endpoints ---


@app.get("/", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "message": "LA server running"}


@app.post("/register", response_model=RegisterResponse)
async def register(request: RegisterRequest):
    """
    Register a Mac device.
    Called by the Mac app on first setup OR to re-register after server restart.

    The client provides its own device_id (a UUID generated once on the Mac).
    This allows the QR code to remain valid even if the server restarts.
    """
    # Clean up old devices before registering
    cleanup_old_devices()

    # Validate device_id format (basic check)
    if not request.device_id or len(request.device_id) < 8:
        raise HTTPException(status_code=400, detail="Invalid device_id")

    # Generate a new token for this session
    device_token = generate_token(32)

    # Store/update the device (overwrites if already exists)
    devices[request.device_id] = {
        "pin_hash": request.pin_hash,
        "device_token": device_token,
        "queue": [],
        "last_seen": time.time(),
    }

    return {"device_token": device_token}


@app.post("/command", response_model=CommandResponse)
async def command(request: CommandRequest):
    """
    Queue a command for a device.
    Called by the phone web page.
    """
    # Check device exists
    if request.device_id not in devices:
        raise HTTPException(status_code=404, detail="Device not found")

    device = devices[request.device_id]

    # Verify PIN hash
    if request.pin_hash != device["pin_hash"]:
        raise HTTPException(status_code=403, detail="Invalid PIN")

    # Validate command
    if request.command not in ["ARM", "DISARM"]:
        raise HTTPException(
            status_code=400,
            detail="Invalid command. Must be ARM or DISARM",
        )

    # Check queue size limit to prevent spamming
    if len(device["queue"]) >= MAX_QUEUE_SIZE:
        raise HTTPException(
            status_code=429,
            detail="Too many pending commands. Please wait.",
        )

    # Queue the command
    device["queue"].append(request.command)

    return {"status": "ok"}


@app.post("/poll", response_model=PollResponse)
async def poll(request: PollRequest):
    """
    Poll for pending commands.
    Called by the Mac app every second.
    """
    # Check device exists
    if request.device_id not in devices:
        raise HTTPException(status_code=404, detail="Device not found")

    device = devices[request.device_id]

    # Verify device token
    if request.device_token != device["device_token"]:
        raise HTTPException(status_code=403, detail="Invalid device token")

    # Update heartbeat - keep device alive
    device["last_seen"] = time.time()

    # Check queue
    if device["queue"]:
        cmd = device["queue"].pop(0)
        return {"command": cmd}

    return {"command": None}


# New: analytics endpoint for retention
@app.post("/events", response_model=UsageEventResponse)
async def events(event: UsageEvent):
    """
    Record a simple usage event for retention analytics.
    This is called by the Mac app (never seen by the user).
    """
    allowed_events = {"armed", "disarmed", "alarm_fired"}
    if event.event not in allowed_events:
        raise HTTPException(status_code=400, detail="Invalid event type")

    append_usage_event(event)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=10000)
