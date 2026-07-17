"""
Cluster manager helpers for warp-proxy.

The manager container exposes one HTTP proxy port, one SOCKS5 proxy port,
and one web UI while forwarding traffic to multiple WARP node containers.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from .auth import get_node_basic_auth_header

logger = logging.getLogger(__name__)

DEFAULT_CLUSTER_NODES = (
    "warp-1=http://warp-1:8000,"
    "warp-2=http://warp-2:8000,"
    "warp-3=http://warp-3:8000"
)


@dataclass(frozen=True)
class ClusterNode:
    id: str
    base_url: str


@dataclass
class ProxyTarget:
    label: str
    host: str
    port: int
    healthy: bool = True
    total_connections: int = 0
    failed_connections: int = 0
    bytes_from_clients: int = 0
    bytes_from_targets: int = 0
    last_error: Optional[str] = None
    last_checked_at: Optional[float] = None


def parse_cluster_nodes(value: Optional[str] = None) -> List[ClusterNode]:
    raw = value or os.getenv("CLUSTER_NODES") or DEFAULT_CLUSTER_NODES
    nodes: List[ClusterNode] = []

    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue

        if "=" in entry:
            node_id, base_url = entry.split("=", 1)
            node_id = node_id.strip()
            base_url = base_url.strip()
        else:
            base_url = entry
            parsed = urlparse(base_url)
            node_id = parsed.hostname or base_url

        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Invalid cluster node URL: {entry}")
        if not node_id:
            raise ValueError(f"Invalid cluster node id: {entry}")

        nodes.append(ClusterNode(id=node_id, base_url=base_url.rstrip("/")))

    if not nodes:
        raise ValueError("At least one cluster node is required")
    return nodes


def parse_proxy_targets(
    value: Optional[str],
    default_port: int,
    nodes: Optional[List[ClusterNode]] = None,
) -> List[ProxyTarget]:
    raw = value
    if not raw and nodes:
        parts = []
        for node in nodes:
            parsed = urlparse(node.base_url)
            if parsed.hostname:
                parts.append(f"{node.id}={parsed.hostname}:{default_port}")
        raw = ",".join(parts)

    targets: List[ProxyTarget] = []
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue

        label = entry
        target = entry
        if "=" in entry:
            label, target = entry.split("=", 1)
            label = label.strip()
            target = target.strip()

        if ":" in target:
            host, port_text = target.rsplit(":", 1)
            port = int(port_text)
        else:
            host = target
            port = default_port

        if not host:
            raise ValueError(f"Invalid proxy target: {entry}")
        targets.append(ProxyTarget(label=label or host, host=host, port=port))

    if not targets:
        raise ValueError("At least one proxy target is required")
    return targets


class TargetPool:
    def __init__(self, targets: List[ProxyTarget]):
        self._targets = targets
        self._idx = 0
        self._lock = Lock()

    def next(self) -> ProxyTarget:
        with self._lock:
            healthy_targets = [target for target in self._targets if target.healthy]
            candidates = healthy_targets or self._targets
            target = candidates[self._idx % len(candidates)]
            self._idx += 1
            target.total_connections += 1
            return target

    def mark_success(self, target: ProxyTarget) -> None:
        with self._lock:
            target.healthy = True
            target.last_error = None
            target.last_checked_at = time.time()

    def mark_failure(self, target: ProxyTarget, error: Exception) -> None:
        with self._lock:
            target.healthy = False
            target.failed_connections += 1
            target.last_error = str(error)
            target.last_checked_at = time.time()

    def add_bytes(self, target: ProxyTarget, direction: str, size: int) -> None:
        with self._lock:
            if direction == "client":
                target.bytes_from_clients += size
            else:
                target.bytes_from_targets += size

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "label": target.label,
                    "host": target.host,
                    "port": target.port,
                    "healthy": target.healthy,
                    "total_connections": target.total_connections,
                    "failed_connections": target.failed_connections,
                    "bytes_from_clients": target.bytes_from_clients,
                    "bytes_from_targets": target.bytes_from_targets,
                    "total_bytes": target.bytes_from_clients + target.bytes_from_targets,
                    "last_error": target.last_error,
                    "last_checked_at": target.last_checked_at,
                }
                for target in self._targets
            ]

    def raw_targets(self) -> List[ProxyTarget]:
        with self._lock:
            return list(self._targets)


class TcpLoadBalancer:
    def __init__(
        self,
        name: str,
        bind_host: str,
        bind_port: int,
        targets: List[ProxyTarget],
        health_interval: int = 15,
    ):
        self.name = name
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.targets = TargetPool(targets)
        self.health_interval = health_interval
        self._server: Optional[asyncio.AbstractServer] = None
        self._health_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self.bind_host,
            port=self.bind_port,
            reuse_address=True,
        )
        self._health_task = asyncio.create_task(self._health_loop())
        logger.info(
            "%s load balancer listening on %s:%s",
            self.name,
            self.bind_host,
            self.bind_port,
        )

    async def stop(self) -> None:
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "bind_host": self.bind_host,
            "bind_port": self.bind_port,
            "targets": self.targets.snapshot(),
        }

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        target = self.targets.next()
        peer = client_writer.get_extra_info("peername")

        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                target.host,
                target.port,
            )
            self.targets.mark_success(target)
        except Exception as exc:
            self.targets.mark_failure(target, exc)
            logger.warning(
                "%s target %s:%s unavailable for %s: %s",
                self.name,
                target.host,
                target.port,
                peer,
                exc,
            )
            await self._close_writer(client_writer)
            return

        try:
            await asyncio.gather(
                self._pipe(client_reader, upstream_writer, target, "client"),
                self._pipe(upstream_reader, client_writer, target, "target"),
            )
        finally:
            await self._close_writer(upstream_writer)
            await self._close_writer(client_writer)

    async def _pipe(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        target: ProxyTarget,
        direction: str,
    ) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                self.targets.add_bytes(target, direction, len(data))
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            await self._close_writer(writer)

    async def _close_writer(self, writer: asyncio.StreamWriter) -> None:
        if writer.is_closing():
            return
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    async def _health_loop(self) -> None:
        while True:
            for target in self.targets.raw_targets():
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(target.host, target.port),
                        timeout=3,
                    )
                    await self._close_writer(writer)
                    self.targets.mark_success(target)
                    del reader
                except Exception as exc:
                    self.targets.mark_failure(target, exc)
            await asyncio.sleep(self.health_interval)


class ClusterClient:
    def __init__(self, nodes: List[ClusterNode], timeout: float = 10.0):
        self.nodes = nodes
        self.timeout = timeout
        self.auth_headers = get_node_basic_auth_header()

    def get_node(self, node_id: str) -> ClusterNode:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)

    async def request(
        self,
        node_id: str,
        method: str,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        node = self.get_node(node_id)
        return await self._request_node(node, method, path, json_body=json_body)

    async def summaries(self) -> List[Dict[str, Any]]:
        return await asyncio.gather(*(self._summary(node) for node in self.nodes))

    async def post_all(self, path: str) -> List[Dict[str, Any]]:
        return await asyncio.gather(
            *(self._request_node(node, "POST", path) for node in self.nodes)
        )

    async def _summary(self, node: ClusterNode) -> Dict[str, Any]:
        status, licenses = await asyncio.gather(
            self._request_node(node, "GET", "/api/status"),
            self._request_node(node, "GET", "/api/licenses"),
        )

        license_list = []
        if licenses.get("ok"):
            license_list = licenses.get("data", {}).get("licenses", [])

        return {
            "id": node.id,
            "base_url": node.base_url,
            "reachable": bool(status.get("ok")),
            "status": status.get("data") if status.get("ok") else None,
            "licenses": license_list,
            "license_count": len(license_list),
            "error": status.get("error") if not status.get("ok") else None,
        }

    async def _request_node(
        self,
        node: ClusterNode,
        method: str,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{node.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(
                    method,
                    url,
                    json=json_body,
                    headers=self.auth_headers,
                )
            if response.status_code >= 400:
                return {
                    "ok": False,
                    "node_id": node.id,
                    "status_code": response.status_code,
                    "error": response.text,
                }
            return {
                "ok": True,
                "node_id": node.id,
                "status_code": response.status_code,
                "data": response.json(),
            }
        except Exception as exc:
            return {
                "ok": False,
                "node_id": node.id,
                "status_code": 0,
                "error": str(exc),
            }
