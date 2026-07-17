import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.warp_manager as warp_manager


class WarpManagerTests(unittest.TestCase):
    def _cmd_result(self, stdout="", stderr="", returncode=0):
        return subprocess.CompletedProcess(["warp-cli"], returncode, stdout, stderr)

    def test_disconnected_status_is_not_reported_as_connected(self):
        with patch.object(
            warp_manager,
            "_run_cmd",
            return_value=self._cmd_result(stdout="Status update: Disconnected"),
        ):
            self.assertEqual(warp_manager._get_warp_status(), "disconnected")

    def test_connected_status_is_detected(self):
        with patch.object(
            warp_manager,
            "_run_cmd",
            return_value=self._cmd_result(stdout="Status update: Connected"),
        ):
            self.assertEqual(warp_manager._get_warp_status(), "connected")

    def test_json_roundtrip_uses_utf8_and_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "settings.json"
            data = {"proxy_user": "warp", "note": "中文"}

            warp_manager._save_json(path, data)

            self.assertEqual(warp_manager._load_json(path), data)
            self.assertTrue(path.read_text(encoding="utf-8").endswith("\n"))

    def test_license_index_is_sanitized_when_file_shape_is_invalid(self):
        old_index_path = warp_manager.LICENSES_INDEX
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "licenses" / "index.json"
                path.parent.mkdir(parents=True)
                path.write_text('{"licenses": {}, "last_id": "bad"}', encoding="utf-8")
                warp_manager.LICENSES_INDEX = path

                index = warp_manager.load_license_index()

                self.assertEqual(index, {"licenses": [], "last_id": 0})
        finally:
            warp_manager.LICENSES_INDEX = old_index_path


if __name__ == "__main__":
    unittest.main()
