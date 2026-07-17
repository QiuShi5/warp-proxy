"""
warp-proxy - WARP Manager Core Module

Manages Cloudflare WARP lifecycle with license pool rotation:
  - Auto-generate anonymous WARP registrations (license pool)
  - Rotate between registrations to change egress IP
  - Health checks and automatic recovery
  - Connection status monitoring
"""

import os
import json
import time
import uuid
import shutil
import subprocess
import threading
import logging
import ipaddress
from pathlib import Path
from functools import wraps
from datetime import datetime
from typing import Optional, List

logger = logging.getLogger(__name__)

# ?? Paths ????????????????????????????????????????????????????????????
WARP_DATA_DIR = Path("/var/lib/cloudflare-warp")
DATA_DIR = Path("/data")
LICENSES_DIR = DATA_DIR / "licenses"
LICENSES_INDEX = LICENSES_DIR / "index.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
CURRENT_LICENSE_FILE = DATA_DIR / "current_license_id"

# ?? Default configuration ???????????????????????????????????????????
DEFAULT_SETTINGS = {
    "refresh_interval_minutes": 0,   # 0 = disabled
    "auto_rotate": False,
    "proxy_user": "",
    "proxy_pass": "",
    "health_check_interval": 60,      # seconds
}

WARP_SOCKS5_ADDR = "127.0.0.1:40000"
WARP_OPERATION_LOCK = threading.RLock()


# ?? Helpers ??????????????????????????????????????????????????????????

def _run_cmd(cmd: list, timeout: int = 30, check: bool = False) -> subprocess.CompletedProcess:
    """Run a shell command and return the CompletedProcess."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and result.returncode != 0:
            logger.warning(f"Command {' '.join(cmd)} failed (rc={result.returncode}): {result.stderr.strip()}")
        return result
    except subprocess.TimeoutExpired:
        logger.warning(f"Command {' '.join(cmd)} timed out after {timeout}s")
        return subprocess.CompletedProcess(cmd, -1, "", "TIMEOUT")
    except FileNotFoundError:
        logger.error(f"Command not found: {cmd[0]}")
        return subprocess.CompletedProcess(cmd, -1, "", "NOT_FOUND")


def _warp_operation(func):
    """Serialize operations that mutate WARP registration data or daemon state."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        with WARP_OPERATION_LOCK:
            return func(*args, **kwargs)
    return wrapper


def _get_warp_status() -> str:
    """Query warp-cli status and return the connection status string."""
    r = _run_cmd(["warp-cli", "--accept-tos", "status"])
    output = (r.stdout + r.stderr).lower()
    if "connected" in output and "disconnected" not in output:
        return "connected"
    elif "disconnected" in output:
        return "disconnected"
    elif "connecting" in output:
        return "connecting"
    else:
        return "unknown"


def _check_external_ip() -> Optional[str]:
    """Check egress IP through the WARP SOCKS5 proxy via curl."""
    for url in ["https://ifconfig.me", "https://api.ipify.org", "https://icanhazip.com"]:
        try:
            r = _run_cmd(
                ["curl", "--socks5", WARP_SOCKS5_ADDR, "--max-time", "8", "-s", url],
                timeout=12
            )
            ip = r.stdout.strip()
            if ip:
                ipaddress.ip_address(ip)
                return ip
        except Exception:
            continue
    return None


def _load_json(path: Path, default=None):
    if default is None:
        default = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_path, path)


def _clear_directory_contents(path: Path):
    """Remove all entries inside a directory without deleting the directory itself."""
    path.mkdir(parents=True, exist_ok=True)
    for item in path.iterdir():
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()


def _copy_directory_contents(source_dir: Path, dest_dir: Path):
    """Copy all entries from source_dir into dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for item in source_dir.iterdir():
        dest = dest_dir / item.name
        if item.is_dir() and not item.is_symlink():
            shutil.copytree(item, dest, symlinks=True)
        else:
            shutil.copy2(item, dest, follow_symlinks=False)


def _stop_warp_svc():
    """Stop the warp-svc daemon."""
    _run_cmd(["pkill", "-f", "warp-svc"], timeout=5)
    time.sleep(2)


def _start_warp_svc():
    """Start warp-svc daemon in background."""
    proc = subprocess.Popen(
        ["warp-svc"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid
    )
    time.sleep(3)
    return proc


def _is_warp_svc_running() -> bool:
    """Check if warp-svc process is running."""
    try:
        r = subprocess.run(["pgrep", "-f", "warp-svc"], capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


# ?? License Pool Management ??????????????????????????????????????????

def load_license_index() -> dict:
    """Load the license pool index."""
    index = _load_json(LICENSES_INDEX, {"licenses": [], "last_id": 0})
    if not isinstance(index, dict):
        index = {"licenses": [], "last_id": 0}
    if not isinstance(index.get("licenses"), list):
        index["licenses"] = []
    if not isinstance(index.get("last_id"), int):
        index["last_id"] = len(index["licenses"])
    return index


def save_license_index(index: dict):
    """Save the license pool index."""
    _save_json(LICENSES_INDEX, index)


def get_current_license_id() -> Optional[str]:
    """Get the currently active license ID."""
    if CURRENT_LICENSE_FILE.exists():
        return CURRENT_LICENSE_FILE.read_text(encoding="utf-8").strip()
    return None


def set_current_license_id(license_id: Optional[str]):
    """Persist the currently active license ID."""
    if license_id:
        CURRENT_LICENSE_FILE.write_text(license_id, encoding="utf-8")
    elif CURRENT_LICENSE_FILE.exists():
        CURRENT_LICENSE_FILE.unlink()


def _license_for_response(license_info: dict, current_id: Optional[str]) -> dict:
    """Return a license payload with current-binding flags normalized."""
    item = dict(license_info)
    item["is_current"] = item.get("id") == current_id
    if not item["is_current"] and item.get("status") == "active":
        item["status"] = "available"
    return item


@_warp_operation
def generate_license() -> dict:
    """
    Generate a new anonymous WARP registration and add it to the pool.

    Steps:
      1. Back up the current registration (temp save)
      2. Disconnect, delete old registration
      3. Register new WARP account
      4. Connect and verify IP
      5. Backup the new registration data
      6. Restore the previous managed registration, or keep the new
         registration active when no managed license was current
      7. Return the new license metadata
    """
    logger.info("Generating new WARP license...")

    # 1. Backup current registration if it exists
    current_backup = None
    current_id = get_current_license_id()
    if current_id and (LICENSES_DIR / current_id / "registration").exists():
        current_backup = current_id
        logger.info(f"Will restore current license [{current_id}] after generation")
    activate_new_license = current_backup is None

    temp_backup = None
    if WARP_DATA_DIR.exists() and any(WARP_DATA_DIR.iterdir()):
        temp_backup = DATA_DIR / ".temp_warp_backup"
        if temp_backup.exists():
            shutil.rmtree(temp_backup)
        shutil.copytree(WARP_DATA_DIR, temp_backup)
        logger.info("Backed up current WARP data temporarily")

    try:
        # 2. Disconnect and delete old registration
        _run_cmd(["warp-cli", "--accept-tos", "disconnect"], timeout=10)
        time.sleep(2)
        _run_cmd(["warp-cli", "--accept-tos", "registration", "delete"], timeout=10)
        time.sleep(2)

        # Stop warp-svc so we can clean the data dir cleanly
        _stop_warp_svc()

        # Remove old registration data
        _clear_directory_contents(WARP_DATA_DIR)

        # 3. Start warp-svc and create new registration
        _start_warp_svc()
        r = _run_cmd(["warp-cli", "--accept-tos", "registration", "new"], timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"Failed to create new registration: {r.stderr}")

        # Set mode to proxy
        _run_cmd(["warp-cli", "--accept-tos", "mode", "proxy"], timeout=10)

        # 4. Connect and verify
        _run_cmd(["warp-cli", "--accept-tos", "connect"], timeout=15)
        time.sleep(8)

        # Poll for connection
        connected = False
        for _ in range(15):
            status = _get_warp_status()
            if status == "connected":
                connected = True
                break
            time.sleep(2)

        initial_ip = None
        if connected:
            initial_ip = _check_external_ip()
            logger.info(f"New WARP registration connected, IP: {initial_ip}")
        else:
            logger.warning("New WARP registration could not connect")

        # 5. Backup the new registration data
        new_id = str(uuid.uuid4())
        new_license_dir = LICENSES_DIR / new_id / "registration"
        new_license_dir.mkdir(parents=True, exist_ok=True)

        # Copy WARP data
        if WARP_DATA_DIR.exists():
            _copy_directory_contents(WARP_DATA_DIR, new_license_dir)

        status = "active" if connected and activate_new_license else "available" if connected else "unverified"

        # Save metadata
        meta = {
            "id": new_id,
            "created_at": datetime.utcnow().isoformat(),
            "initial_ip": initial_ip,
            "connected": connected,
            "status": status,
        }
        _save_json(LICENSES_DIR / new_id / "meta.json", meta)

        # Add to index
        index = load_license_index()
        index["last_id"] += 1
        index["licenses"].append({
            "id": new_id,
            "seq": index["last_id"],
            "label": f"License #{index['last_id']}",
            "created_at": meta["created_at"],
            "initial_ip": initial_ip,
            "connected": connected,
            "status": meta["status"],
        })
        save_license_index(index)

        logger.info(f"New license [{new_id}] generated and added to pool")

        # 6. Restore previous registration if needed
        if current_backup:
            switch_to_license(current_backup, restore_mode=True)
        else:
            set_current_license_id(new_id)
            logger.info(f"License [{new_id}] is now the current managed license")

        return {"success": True, "license_id": new_id, "ip": initial_ip}

    except Exception as e:
        logger.error(f"Failed to generate license: {e}")
        # Try to restore from temp backup
        if temp_backup and temp_backup.exists():
            logger.info("Restoring from temp backup due to error")
            _restore_data_dir(temp_backup)
        raise

    finally:
        # Clean up temp backup
        if temp_backup and temp_backup.exists():
            shutil.rmtree(temp_backup, ignore_errors=True)


def _restore_data_dir(source_dir: Path):
    """Restore WARP data directory from a source backup."""
    _stop_warp_svc()
    _clear_directory_contents(WARP_DATA_DIR)
    _copy_directory_contents(source_dir, WARP_DATA_DIR)
    _start_warp_svc()
    time.sleep(3)


@_warp_operation
def switch_to_license(license_id: str, restore_mode: bool = False) -> dict:
    """
    Switch the active WARP registration to the specified license.

    Steps:
      1. Disconnect WARP
      2. Delete current registration
      3. Stop warp-svc
      4. Replace WARP data directory with license backup
      5. Start warp-svc
      6. Connect and verify IP
      7. Update current license tracking
    """
    logger.info(f"Switching to license [{license_id}]...")

    license_dir = LICENSES_DIR / license_id / "registration"
    if not license_dir.exists():
        raise FileNotFoundError(f"License data not found for [{license_id}]")

    # 1. Disconnect
    _run_cmd(["warp-cli", "--accept-tos", "disconnect"], timeout=10)
    time.sleep(2)

    # 2. Delete registration
    _run_cmd(["warp-cli", "--accept-tos", "registration", "delete"], timeout=10)
    time.sleep(1)

    # 3. Stop warp-svc
    _stop_warp_svc()

    # 4. Replace data directory
    _clear_directory_contents(WARP_DATA_DIR)
    _copy_directory_contents(license_dir, WARP_DATA_DIR)

    # 5. Start warp-svc
    _start_warp_svc()

    # 6. Set mode and connect
    _run_cmd(["warp-cli", "--accept-tos", "mode", "proxy"], timeout=10)
    _run_cmd(["warp-cli", "--accept-tos", "connect"], timeout=15)
    time.sleep(5)

    # Poll for connection
    connected = False
    for _ in range(15):
        status = _get_warp_status()
        if status == "connected":
            connected = True
            break
        time.sleep(2)

    new_ip = None
    if connected:
        new_ip = _check_external_ip()
        logger.info(f"License [{license_id}] connected, IP: {new_ip}")
    else:
        logger.warning(f"License [{license_id}] failed to connect after switch")

    # 7. Update tracking
    set_current_license_id(license_id)

    # Update license status in index
    index = load_license_index()
    for lic in index["licenses"]:
        if lic["id"] == license_id:
            lic["status"] = "active" if connected else "error"
            lic["last_ip"] = new_ip
        elif lic.get("status") == "active":
            lic["status"] = "available"
    save_license_index(index)

    return {
        "success": True,
        "license_id": license_id,
        "ip": new_ip,
        "connected": connected,
    }


@_warp_operation
def delete_license(license_id: str) -> dict:
    """Remove a license from the pool."""
    lic_dir = LICENSES_DIR / license_id
    if not lic_dir.exists():
        raise FileNotFoundError(f"License [{license_id}] not found")

    # Check if currently active
    current_id = get_current_license_id()
    if current_id == license_id:
        raise ValueError(f"Cannot delete the currently active license [{license_id}]. Switch to another first.")

    # Remove from index
    index = load_license_index()
    index["licenses"] = [l for l in index["licenses"] if l["id"] != license_id]
    save_license_index(index)

    # Remove data
    shutil.rmtree(lic_dir)

    logger.info(f"License [{license_id}] deleted")
    return {"success": True, "license_id": license_id}


def list_licenses() -> List[dict]:
    """List all licenses in the pool."""
    index = load_license_index()
    current_id = get_current_license_id()
    return [_license_for_response(lic, current_id) for lic in index["licenses"]]


def get_license_detail(license_id: str) -> Optional[dict]:
    """Get detailed info for a specific license."""
    meta_path = LICENSES_DIR / license_id / "meta.json"
    if not meta_path.exists():
        return None
    meta = _load_json(meta_path)
    current_id = get_current_license_id()
    return _license_for_response(meta, current_id)


# ?? WARP Connection Management ??????????????????????????????????????

def get_status() -> dict:
    """Get comprehensive current status of the WARP proxy."""
    warp_status = _get_warp_status()
    current_id = get_current_license_id()
    settings = get_settings()

    # Get IP (only if connected)
    ip = None
    if warp_status == "connected":
        ip = _check_external_ip()

    result = {
        "warp_status": warp_status,
        "service_running": _is_warp_svc_running(),
        "current_license_id": current_id,
        "external_ip": ip,
        "checked_at": datetime.utcnow().isoformat(),
    }

    # If we have a current license, find its details
    if current_id:
        index = load_license_index()
        for lic in index["licenses"]:
            if lic["id"] == current_id:
                result["current_license"] = lic
                break

    return result


@_warp_operation
def connect_warp() -> dict:
    """Connect to WARP network."""
    _run_cmd(["warp-cli", "--accept-tos", "connect"], timeout=15)
    time.sleep(5)
    for _ in range(10):
        status = _get_warp_status()
        if status == "connected":
            ip = _check_external_ip()
            return {"success": True, "status": "connected", "ip": ip}
        time.sleep(2)
    return {"success": False, "status": _get_warp_status(), "ip": None}


@_warp_operation
def disconnect_warp() -> dict:
    """Disconnect from WARP network."""
    _run_cmd(["warp-cli", "--accept-tos", "disconnect"], timeout=10)
    time.sleep(2)
    status = _get_warp_status()
    return {"success": status == "disconnected", "status": status}


@_warp_operation
def rotate_license() -> dict:
    """
    Rotate to the next available license in the pool.
    Cycles through the pool sequentially.
    """
    index = load_license_index()
    licenses = index.get("licenses", [])
    if not licenses:
        raise RuntimeError("No licenses in pool. Generate at least one first.")

    current_id = get_current_license_id()

    # Find current position in pool
    current_idx = -1
    for i, lic in enumerate(licenses):
        if lic["id"] == current_id:
            current_idx = i
            break

    # Get next license (or first if current not found)
    next_idx = (current_idx + 1) % len(licenses)
    next_license = licenses[next_idx]

    return switch_to_license(next_license["id"])


# ?? Settings ?????????????????????????????????????????????????????????

def get_settings() -> dict:
    """Get current settings."""
    settings = _load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    return {**DEFAULT_SETTINGS, **settings}


def update_settings(new_settings: dict) -> dict:
    """Update settings."""
    current = get_settings()
    # Only allow updating known keys
    for key in new_settings:
        if key in DEFAULT_SETTINGS:
            current[key] = new_settings[key]
    _save_json(SETTINGS_FILE, current)
    return current


# ?? Background Tasks ????????????????????????????????????????????????

class HealthCheckLoop:
    """Background thread for periodic health checks and auto-recovery."""

    def __init__(self):
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Health check loop started")

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        logger.info("Health check loop stopped")

    def _run(self):
        while not self._stop_event.is_set():
            wait_seconds = DEFAULT_SETTINGS["health_check_interval"]
            try:
                settings = get_settings()
                wait_seconds = max(10, int(settings.get("health_check_interval", wait_seconds)))
                self._check_and_recover()
            except Exception as e:
                logger.error(f"Health check error: {e}")
            self._stop_event.wait(wait_seconds)

    def _check_and_recover(self):
        if not WARP_OPERATION_LOCK.acquire(blocking=False):
            logger.debug("Skipping health check while another WARP operation is running")
            return
        try:
            status = _get_warp_status()
            svc_running = _is_warp_svc_running()

            # If warp-svc is not running, try to restart
            if not svc_running:
                logger.warning("warp-svc is not running, attempting restart...")
                _start_warp_svc()
                time.sleep(3)
                _run_cmd(["warp-cli", "--accept-tos", "connect"], timeout=15)
                return

            # If disconnected but should be connected, try reconnect
            if status == "disconnected":
                logger.warning("WARP disconnected, attempting reconnect...")
                _run_cmd(["warp-cli", "--accept-tos", "connect"], timeout=15)
                time.sleep(5)
        finally:
            WARP_OPERATION_LOCK.release()


class AutoRefreshLoop:
    """Background thread for periodic IP refresh (license rotation at interval)."""

    def __init__(self):
        self._stop_event = threading.Event()
        self._thread = None
        self._last_rotate = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Auto-refresh loop started")

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        logger.info("Auto-refresh loop stopped")

    def _run(self):
        while not self._stop_event.is_set():
            try:
                settings = get_settings()
                interval = max(0, int(settings.get("refresh_interval_minutes", 0)))
                enabled = bool(settings.get("auto_rotate", False))
                if enabled and interval > 0:
                    now = time.time()
                    if self._last_rotate == 0:
                        self._last_rotate = now
                    elif (now - self._last_rotate) >= interval * 60:
                        try:
                            logger.info(f"Auto-rotate: refreshing license (interval={interval}min)")
                            result = rotate_license()
                            self._last_rotate = now
                            if result.get("ip"):
                                logger.info(f"Auto-rotate complete, new IP: {result['ip']}")
                        except Exception as e:
                            logger.error(f"Auto-rotate failed: {e}")
                else:
                    self._last_rotate = 0
                self._stop_event.wait(60)
            except Exception as e:
                logger.error(f"Refresh loop error: {e}")
                self._stop_event.wait(30)


# Global instances
health_checker = HealthCheckLoop()
refresh_loop = AutoRefreshLoop()


def start_background_tasks():
    """Start background health check and auto-refresh threads."""
    health_checker.start()
    refresh_loop.start()


def stop_background_tasks():
    """Stop background threads."""
    health_checker.stop()
    refresh_loop.stop()
