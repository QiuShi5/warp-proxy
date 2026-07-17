"""
Unified cluster entrypoint for warp-proxy.

This app runs in the manager container. It exposes:
  - TCP load balancer for SOCKS5 on port 1080
  - TCP load balancer for HTTP proxy on port 8080
  - Unified FastAPI web UI/API on port 8000
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from .auth import install_auth
from .cluster_manager import (
    ClusterClient,
    TcpLoadBalancer,
    parse_cluster_nodes,
    parse_proxy_targets,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
DASHBOARD_HTML = STATIC_DIR / "dashboard.html"

nodes = parse_cluster_nodes()
cluster_client = ClusterClient(nodes)

socks_balancer = TcpLoadBalancer(
    name="socks5",
    bind_host=os.getenv("SOCKS_BIND_HOST", "0.0.0.0"),
    bind_port=int(os.getenv("SOCKS_BIND_PORT", "1080")),
    targets=parse_proxy_targets(os.getenv("SOCKS_TARGETS"), 1080, nodes),
)

http_balancer = TcpLoadBalancer(
    name="http",
    bind_host=os.getenv("HTTP_BIND_HOST", "0.0.0.0"),
    bind_port=int(os.getenv("HTTP_BIND_PORT", "8080")),
    targets=parse_proxy_targets(os.getenv("HTTP_TARGETS"), 8080, nodes),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting cluster manager...")
    await socks_balancer.start()
    await http_balancer.start()
    try:
        yield
    finally:
        logger.info("Stopping cluster manager...")
        await http_balancer.stop()
        await socks_balancer.stop()


app = FastAPI(title="warp-proxy-cluster", version="1.0.0", lifespan=lifespan)
install_auth(app)


def _unwrap_node_response(result: Dict[str, Any]) -> Dict[str, Any]:
    if result.get("ok"):
        return result.get("data", {})
    raise HTTPException(
        status_code=502,
        detail={
            "node_id": result.get("node_id"),
            "status_code": result.get("status_code"),
            "error": result.get("error"),
        },
    )


def _cluster_summary(nodes_status: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_nodes = len(nodes_status)
    reachable_nodes = sum(1 for node in nodes_status if node.get("reachable"))
    connected_nodes = 0
    total_licenses = 0

    for node in nodes_status:
        total_licenses += int(node.get("license_count") or 0)
        status = node.get("status") or {}
        if status.get("warp_status") == "connected":
            connected_nodes += 1

    return {
        "total_nodes": total_nodes,
        "reachable_nodes": reachable_nodes,
        "connected_nodes": connected_nodes,
        "total_licenses": total_licenses,
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    if DASHBOARD_HTML.exists():
        return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>warp-proxy cluster</h1><p>Frontend not found.</p>")


@app.get("/api/health")
async def api_health():
    return {
        "status": "ok",
        "nodes": [node.id for node in nodes],
        "balancers": {
            "socks5": socks_balancer.snapshot(),
            "http": http_balancer.snapshot(),
        },
    }


async def _dashboard_payload():
    node_status = await cluster_client.summaries()
    return {
        "summary": _cluster_summary(node_status),
        "nodes": node_status,
        "balancers": {
            "socks5": socks_balancer.snapshot(),
            "http": http_balancer.snapshot(),
        },
    }


@app.get("/api/dashboard")
async def api_dashboard():
    return await _dashboard_payload()


@app.get("/api/cluster/status")
async def api_cluster_status():
    return await _dashboard_payload()


@app.post("/api/cluster/rotate")
async def api_cluster_rotate():
    return {"results": await cluster_client.post_all("/api/rotate")}


@app.post("/api/cluster/licenses/generate")
async def api_cluster_generate_license():
    return {"results": await cluster_client.post_all("/api/licenses/generate")}


@app.get("/api/nodes/{node_id}/status")
async def api_node_status(node_id: str):
    return _unwrap_node_response(await cluster_client.request(node_id, "GET", "/api/status"))


@app.get("/api/nodes/{node_id}/licenses")
async def api_node_licenses(node_id: str):
    return _unwrap_node_response(await cluster_client.request(node_id, "GET", "/api/licenses"))


@app.get("/api/nodes/{node_id}/settings")
async def api_node_settings(node_id: str):
    return _unwrap_node_response(await cluster_client.request(node_id, "GET", "/api/settings"))


@app.post("/api/nodes/{node_id}/settings")
async def api_node_update_settings(node_id: str, request: Request):
    return _unwrap_node_response(
        await cluster_client.request(
            node_id,
            "POST",
            "/api/settings",
            json_body=await request.json(),
        )
    )


@app.get("/api/nodes/{node_id}/logs")
async def api_node_logs(node_id: str, tail: int = Query(default=200, ge=1, le=1000)):
    return _unwrap_node_response(
        await cluster_client.request(node_id, "GET", f"/api/logs?tail={tail}")
    )


@app.post("/api/nodes/{node_id}/logs/clear")
async def api_node_clear_logs(node_id: str):
    return _unwrap_node_response(
        await cluster_client.request(node_id, "POST", "/api/logs/clear")
    )


@app.post("/api/nodes/{node_id}/connect")
async def api_node_connect(node_id: str):
    return _unwrap_node_response(await cluster_client.request(node_id, "POST", "/api/connect"))


@app.post("/api/nodes/{node_id}/disconnect")
async def api_node_disconnect(node_id: str):
    return _unwrap_node_response(await cluster_client.request(node_id, "POST", "/api/disconnect"))


@app.post("/api/nodes/{node_id}/rotate")
async def api_node_rotate(node_id: str):
    return _unwrap_node_response(await cluster_client.request(node_id, "POST", "/api/rotate"))


@app.post("/api/nodes/{node_id}/licenses/generate")
async def api_node_generate_license(node_id: str):
    return _unwrap_node_response(
        await cluster_client.request(node_id, "POST", "/api/licenses/generate")
    )


@app.post("/api/nodes/{node_id}/licenses/{license_id}/activate")
async def api_node_activate_license(node_id: str, license_id: str):
    return _unwrap_node_response(
        await cluster_client.request(
            node_id,
            "POST",
            f"/api/licenses/{license_id}/activate",
        )
    )


@app.delete("/api/nodes/{node_id}/licenses/{license_id}")
async def api_node_delete_license(node_id: str, license_id: str):
    return _unwrap_node_response(
        await cluster_client.request(
            node_id,
            "DELETE",
            f"/api/licenses/{license_id}",
        )
    )
