import logging
import threading
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import backend.app as single_app
import backend.cluster_app as cluster_app


class SingleDashboardTests(unittest.TestCase):
    def test_log_buffer_handle_does_not_deadlock(self):
        buffer = single_app.LogBuffer()
        record = logging.LogRecord(
            "backend.warp_manager",
            logging.INFO,
            __file__,
            1,
            "startup log",
            (),
            None,
        )

        thread = threading.Thread(target=buffer.handle, args=(record,), daemon=True)
        thread.start()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(buffer.get_logs(), ["startup log"])

    def test_single_dashboard_payload_uses_one_node_shape(self):
        licenses = [{"id": "license-1", "is_current": True}]
        status = {
            "warp_status": "connected",
            "external_ip": "203.0.113.10",
            "current_license_id": "license-1",
        }

        with patch.object(single_app, "get_status", return_value=status), patch.object(
            single_app, "list_licenses", return_value=licenses
        ):
            payload = single_app._single_node_dashboard()

        self.assertEqual(payload["summary"]["total_nodes"], 1)
        self.assertEqual(payload["summary"]["connected_nodes"], 1)
        self.assertEqual(payload["summary"]["total_licenses"], 1)
        self.assertEqual(len(payload["nodes"]), 1)
        self.assertEqual(payload["nodes"][0]["licenses"], licenses)
        self.assertEqual(set(payload["balancers"].keys()), {"socks5", "http"})

    def test_single_dashboard_rejects_unknown_node_id(self):
        with self.assertRaises(HTTPException) as ctx:
            single_app._require_single_node("missing-node")

        self.assertEqual(ctx.exception.status_code, 404)


class ClusterDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_cluster_dashboard_and_legacy_status_share_payload_shape(self):
        node_status = [
            {
                "id": "warp-1",
                "base_url": "http://warp-1:8000",
                "reachable": True,
                "status": {"warp_status": "connected"},
                "licenses": [{"id": "license-1"}],
                "license_count": 1,
                "error": None,
            },
            {
                "id": "warp-2",
                "base_url": "http://warp-2:8000",
                "reachable": False,
                "status": None,
                "licenses": [],
                "license_count": 0,
                "error": "timeout",
            },
        ]
        balancer = {
            "name": "socks5",
            "bind_host": "0.0.0.0",
            "bind_port": 1080,
            "running": True,
            "targets": [],
        }

        with patch.object(
            cluster_app.cluster_client, "summaries", AsyncMock(return_value=node_status)
        ), patch.object(
            cluster_app.socks_balancer, "snapshot", return_value=balancer
        ), patch.object(
            cluster_app.http_balancer, "snapshot", return_value={**balancer, "name": "http"}
        ):
            payload = await cluster_app.api_dashboard()
            legacy_payload = await cluster_app.api_cluster_status()

        self.assertEqual(payload, legacy_payload)
        self.assertEqual(payload["summary"]["total_nodes"], 2)
        self.assertEqual(payload["summary"]["reachable_nodes"], 1)
        self.assertEqual(payload["summary"]["connected_nodes"], 1)
        self.assertEqual(payload["summary"]["total_licenses"], 1)
        self.assertEqual(payload["nodes"], node_status)
        self.assertEqual(set(payload["balancers"].keys()), {"socks5", "http"})


if __name__ == "__main__":
    unittest.main()
