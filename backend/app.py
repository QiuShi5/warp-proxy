"""
warp-proxy - FastAPI Backend

Provides REST API for WARP proxy management web UI.
"""

import logging
import os
import threading
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from typing import Any, Dict, Optional

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
DASHBOARD_HTML = STATIC_DIR / "dashboard.html"
SINGLE_NODE_ID = os.getenv("WARP_NODE_ID", os.getenv("NODE_ID", "warp-node-1"))


# ?? Log capture (in-memory ring buffer) ?????????????????????????????
class LogBuffer(logging.Handler):
    """Ring buffer that captures WARP manager logs for the web UI."""

    def __init__(self, max_lines=500):
        super().__init__()
        self.max_lines = max_lines
        self.buffer = []
        self._buffer_lock = threading.Lock()

    def emit(self, record):
        entry = self.format(record)
        with self._buffer_lock:
            self.buffer.append(entry)
            if len(self.buffer) > self.max_lines:
                self.buffer = self.buffer[-self.max_lines:]

    def get_logs(self, tail: int = 200) -> list:
        with self._buffer_lock:
            return self.buffer[-tail:]

    def clear(self):
        with self._buffer_lock:
            self.buffer.clear()


log_buffer = LogBuffer()
log_buffer.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger("backend.warp_manager").addHandler(log_buffer)


# ?? App lifespan ????????????????????????????????????????????????????

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting warp-proxy backend...")
    start_background_tasks()
    yield
    logger.info("Shutting down warp-proxy backend...")
    stop_background_tasks()


app = FastAPI(title="warp-proxy", version="1.0.0", lifespan=lifespan)


# ?? Static files & root route ???????????????????????????????????????

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the web management UI."""
    if DASHBOARD_HTML.exists():
        return HTMLResponse(content=DASHBOARD_HTML.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>warp-proxy</h1><p>Frontend not found.</p>")


# ?? Status endpoints ????????????????????????????????????????????????

@app.get("/api/status")
async def api_status():
    """Get current WARP proxy status."""
    try:
        return get_status()
    except Exception as e:
        logger.error(f"Status check error: {e}")
        return {"warp_status": "error", "error": str(e)}


def _proxy_snapshot(name: str, port: int, connected: bool) -> Dict[str, Any]:
    return {
        "name": name,
        "bind_host": "0.0.0.0",
        "bind_port": port,
        "running": True,
        "targets": [
            {
                "label": SINGLE_NODE_ID,
                "host": "127.0.0.1",
                "port": port,
                "healthy": connected,
                "total_connections": 0,
                "failed_connections": 0,
                "bytes_from_clients": 0,
                "bytes_from_targets": 0,
                "total_bytes": 0,
                "last_error": None,
                "last_checked_at": None,
            }
        ],
    }


def _single_node_dashboard() -> Dict[str, Any]:
    status = get_status()
    licenses = list_licenses()
    connected = status.get("warp_status") == "connected"
    node = {
        "id": SINGLE_NODE_ID,
        "base_url": "local",
        "reachable": True,
        "status": status,
        "licenses": licenses,
        "license_count": len(licenses),
        "error": None,
    }
    return {
        "summary": {
            "total_nodes": 1,
            "reachable_nodes": 1,
            "connected_nodes": 1 if connected else 0,
            "total_licenses": len(licenses),
        },
        "nodes": [node],
        "balancers": {
            "socks5": _proxy_snapshot("socks5", 1080, connected),
            "http": _proxy_snapshot("http", 8080, connected),
        },
    }


def _require_single_node(node_id: str) -> None:
    if node_id != SINGLE_NODE_ID:
        raise HTTPException(404, f"Node [{node_id}] not found")


def _node_result(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "results": [
            {
                "ok": True,
                "node_id": SINGLE_NODE_ID,
                "status_code": 200,
                "data": data,
            }
        ]
    }


@app.get("/api/dashboard")
async def api_dashboard():
    """Return the shared dashboard payload."""
    try:
        return _single_node_dashboard()
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/cluster/rotate")
async def api_cluster_rotate():
    """Single-node compatibility endpoint for the shared dashboard."""
    try:
        return _node_result(rotate_license())
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/cluster/licenses/generate")
async def api_cluster_generate_license():
    """Single-node compatibility endpoint for the shared dashboard."""
    try:
        return _node_result(generate_license())
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/connect")
async def api_connect():
    """Connect to WARP."""
    try:
        return connect_warp()
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/nodes/{node_id}/status")
async def api_node_status(node_id: str):
    _require_single_node(node_id)
    return await api_status()


@app.get("/api/nodes/{node_id}/licenses")
async def api_node_licenses(node_id: str):
    _require_single_node(node_id)
    return await api_list_licenses()


@app.post("/api/nodes/{node_id}/licenses/generate")
async def api_node_generate_license(node_id: str):
    _require_single_node(node_id)
    return await api_generate_license()


@app.post("/api/nodes/{node_id}/connect")
async def api_node_connect(node_id: str):
    _require_single_node(node_id)
    return await api_connect()


@app.post("/api/nodes/{node_id}/disconnect")
async def api_node_disconnect(node_id: str):
    _require_single_node(node_id)
    return await api_disconnect()


@app.post("/api/nodes/{node_id}/rotate")
async def api_node_rotate(node_id: str):
    _require_single_node(node_id)
    return await api_rotate()


@app.post("/api/disconnect")
async def api_disconnect():
    """Disconnect from WARP."""
    try:
        return disconnect_warp()
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/rotate")
async def api_rotate():
    """Replace the node's current license with a newly generated one."""
    try:
        return rotate_license()
    except Exception as e:
        raise HTTPException(500, str(e))


# ?? License pool endpoints ??????????????????????????????????????????

@app.get("/api/licenses")
async def api_list_licenses():
    """List all licenses in the pool."""
    try:
        return {"licenses": list_licenses()}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/licenses/generate")
async def api_generate_license():
    """Generate a new anonymous WARP license into the idle pool."""
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


@app.post("/api/nodes/{node_id}/licenses/{license_id}/activate")
async def api_node_activate_license(node_id: str, license_id: str):
    _require_single_node(node_id)
    return await api_activate_license(license_id)


@app.delete("/api/nodes/{node_id}/licenses/{license_id}")
async def api_node_delete_license(node_id: str, license_id: str):
    _require_single_node(node_id)
    return await api_delete_license(license_id)


# ?? Settings endpoints ??????????????????????????????????????????????

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


@app.get("/api/nodes/{node_id}/settings")
async def api_node_settings(node_id: str):
    _require_single_node(node_id)
    return await api_get_settings()


@app.post("/api/nodes/{node_id}/settings")
async def api_node_update_settings(node_id: str, settings: SettingsUpdate):
    _require_single_node(node_id)
    return await api_update_settings(settings)


# ?? Logs endpoint ???????????????????????????????????????????????????

@app.get("/api/logs")
async def api_logs(tail: int = Query(default=200, ge=1, le=1000)):
    """Get recent operation logs."""
    return {"logs": log_buffer.get_logs(tail)}


@app.post("/api/logs/clear")
async def api_clear_logs():
    """Clear operation logs."""
    log_buffer.clear()
    return {"success": True}


@app.get("/api/nodes/{node_id}/logs")
async def api_node_logs(node_id: str, tail: int = Query(default=200, ge=1, le=1000)):
    _require_single_node(node_id)
    return await api_logs(tail=tail)


@app.post("/api/nodes/{node_id}/logs/clear")
async def api_node_clear_logs(node_id: str):
    _require_single_node(node_id)
    return await api_clear_logs()


# ?? Health ??????????????????????????????????????????????????????????

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


# ?? Main entry point (for direct execution) ?????????????????????????

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.app:app", host="0.0.0.0", port=8000, log_level="info")
