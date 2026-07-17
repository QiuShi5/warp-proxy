"""
warp-proxy - FastAPI Backend

Provides REST API for WARP proxy management web UI.
"""

import logging
import threading
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from typing import Optional

from .warp_manager import (
    get_status,
    connect_warp,
    disconnect_warp,
    rotate_license,
    switch_to_license,
    generate_license,
    delete_license,
    list_licenses,
    get_license_detail,
    get_settings,
    update_settings,
    start_background_tasks,
    stop_background_tasks,
    _get_warp_status as warp_raw_status,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


# ── Log capture (in-memory ring buffer) ─────────────────────────────
class LogBuffer(logging.Handler):
    """Ring buffer that captures WARP manager logs for the web UI."""

    def __init__(self, max_lines=500):
        super().__init__()
        self.max_lines = max_lines
        self.buffer = []
        self.lock = threading.Lock()

    def emit(self, record):
        entry = self.format(record)
        with self.lock:
            self.buffer.append(entry)
            if len(self.buffer) > self.max_lines:
                self.buffer = self.buffer[-self.max_lines:]

    def get_logs(self, tail: int = 200) -> list:
        with self.lock:
            return self.buffer[-tail:]

    def clear(self):
        with self.lock:
            self.buffer.clear()


log_buffer = LogBuffer()
log_buffer.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger("backend.warp_manager").addHandler(log_buffer)


# ── App lifespan ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting warp-proxy backend...")
    start_background_tasks()
    yield
    logger.info("Shutting down warp-proxy backend...")
    stop_background_tasks()


app = FastAPI(title="warp-proxy", version="1.0.0", lifespan=lifespan)


# ── Static files & root route ───────────────────────────────────────

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the web management UI."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>warp-proxy</h1><p>Frontend not found.</p>")


# ── Status endpoints ────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    """Get current WARP proxy status."""
    try:
        return get_status()
    except Exception as e:
        logger.error(f"Status check error: {e}")
        return {"warp_status": "error", "error": str(e)}


@app.post("/api/connect")
async def api_connect():
    """Connect to WARP."""
    try:
        return connect_warp()
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/disconnect")
async def api_disconnect():
    """Disconnect from WARP."""
    try:
        return disconnect_warp()
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/rotate")
async def api_rotate():
    """Rotate to next license in pool."""
    try:
        return rotate_license()
    except Exception as e:
        raise HTTPException(500, str(e))


# ── License pool endpoints ──────────────────────────────────────────

@app.get("/api/licenses")
async def api_list_licenses():
    """List all licenses in the pool."""
    try:
        return {"licenses": list_licenses()}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/licenses/generate")
async def api_generate_license():
    """Generate a new anonymous WARP license."""
    try:
        result = generate_license()
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/licenses/{license_id}")
async def api_license_detail(license_id: str):
    """Get details for a specific license."""
    detail = get_license_detail(license_id)
    if not detail:
        raise HTTPException(404, f"License [{license_id}] not found")
    return detail


@app.post("/api/licenses/{license_id}/activate")
async def api_activate_license(license_id: str):
    """Activate (switch to) a specific license."""
    try:
        return switch_to_license(license_id)
    except FileNotFoundError:
        raise HTTPException(404, f"License [{license_id}] not found")
    except Exception as e:
        raise HTTPException(500, str(e))


@app.delete("/api/licenses/{license_id}")
async def api_delete_license(license_id: str):
    """Delete a license from the pool."""
    try:
        return delete_license(license_id)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Settings endpoints ──────────────────────────────────────────────

@app.get("/api/settings")
async def api_get_settings():
    """Get current settings."""
    return get_settings()


class SettingsUpdate(BaseModel):
    refresh_interval_minutes: Optional[int] = Field(default=None, ge=0, le=1440)
    auto_rotate: Optional[bool] = None
    proxy_user: Optional[str] = Field(default=None, max_length=128)
    proxy_pass: Optional[str] = Field(default=None, max_length=512)
    health_check_interval: Optional[int] = Field(default=None, ge=10, le=3600)


@app.post("/api/settings")
async def api_update_settings(settings: SettingsUpdate):
    """Update settings."""
    try:
        updates = {k: v for k, v in settings.model_dump().items() if v is not None}
        result = update_settings(updates)
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Logs endpoint ───────────────────────────────────────────────────

@app.get("/api/logs")
async def api_logs(tail: int = Query(default=200, ge=1, le=1000)):
    """Get recent operation logs."""
    return {"logs": log_buffer.get_logs(tail)}


@app.post("/api/logs/clear")
async def api_clear_logs():
    """Clear operation logs."""
    log_buffer.clear()
    return {"success": True}


# ── Health ──────────────────────────────────────────────────────────

@app.get("/api/health")
async def api_health():
    """Simple health check endpoint."""
    raw = warp_raw_status()
    return {
        "status": "ok",
        "warp_status": raw,
        "service_running": _is_svc_running(),
    }


def _is_svc_running() -> bool:
    try:
        import subprocess
        r = subprocess.run(["pgrep", "-f", "warp-svc"], capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


# ── Main entry point (for direct execution) ─────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.app:app", host="0.0.0.0", port=8000, log_level="info")
