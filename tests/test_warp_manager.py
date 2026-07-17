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
            data = {"proxy_user": "warp", "note": "??"}

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

    def test_clear_directory_contents_keeps_directory_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "warp-data"
            nested = root / "nested"
            nested.mkdir(parents=True)
            (root / "registration.json").write_text("old", encoding="utf-8")
            (nested / "cache").write_text("old", encoding="utf-8")

            warp_manager._clear_directory_contents(root)

            self.assertTrue(root.is_dir())
            self.assertEqual(list(root.iterdir()), [])

    def test_restore_data_dir_does_not_remove_warp_mount_root(self):
        old_warp_data_dir = warp_manager.WARP_DATA_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                warp_root = tmp_path / "warp-data"
                source = tmp_path / "source"
                (warp_root / "old").mkdir(parents=True)
                (warp_root / "old" / "registration").write_text("old", encoding="utf-8")
                (source / "new").mkdir(parents=True)
                (source / "new" / "registration").write_text("new", encoding="utf-8")
                warp_manager.WARP_DATA_DIR = warp_root

                with patch.object(warp_manager, "_stop_warp_svc"), patch.object(
                    warp_manager, "_start_warp_svc"
                ), patch.object(warp_manager.shutil, "rmtree", wraps=warp_manager.shutil.rmtree) as rmtree:
                    warp_manager._restore_data_dir(source)

                self.assertTrue(warp_root.is_dir())
                self.assertFalse((warp_root / "old").exists())
                self.assertEqual((warp_root / "new" / "registration").read_text(encoding="utf-8"), "new")
                self.assertNotIn((warp_root,), [call.args for call in rmtree.call_args_list])
        finally:
            warp_manager.WARP_DATA_DIR = old_warp_data_dir

    def test_first_generated_license_becomes_current_without_restoring_temp_backup(self):
        old_paths = (
            warp_manager.WARP_DATA_DIR,
            warp_manager.DATA_DIR,
            warp_manager.LICENSES_DIR,
            warp_manager.LICENSES_INDEX,
            warp_manager.CURRENT_LICENSE_FILE,
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                warp_root = tmp_path / "warp-data"
                data_root = tmp_path / "data"
                licenses_dir = data_root / "licenses"
                warp_root.mkdir(parents=True)
                (warp_root / "old-registration").write_text("old", encoding="utf-8")
                warp_manager.WARP_DATA_DIR = warp_root
                warp_manager.DATA_DIR = data_root
                warp_manager.LICENSES_DIR = licenses_dir
                warp_manager.LICENSES_INDEX = licenses_dir / "index.json"
                warp_manager.CURRENT_LICENSE_FILE = data_root / "current_license_id"

                def fake_run_cmd(cmd, timeout=30, check=False):
                    if cmd[-2:] == ["registration", "new"]:
                        (warp_root / "new-registration").write_text("new", encoding="utf-8")
                    return self._cmd_result()

                with patch.object(warp_manager, "_run_cmd", side_effect=fake_run_cmd), patch.object(
                    warp_manager, "_get_warp_status", return_value="connected"
                ), patch.object(warp_manager, "_check_external_ip", return_value="203.0.113.10"), patch.object(
                    warp_manager, "_stop_warp_svc"
                ), patch.object(
                    warp_manager, "_start_warp_svc"
                ), patch.object(
                    warp_manager.time, "sleep"
                ), patch.object(
                    warp_manager.uuid, "uuid4", return_value="license-1"
                ):
                    result = warp_manager.generate_license()

                self.assertEqual(result["license_id"], "license-1")
                self.assertEqual(warp_manager.get_current_license_id(), "license-1")
                self.assertFalse((warp_root / "old-registration").exists())
                self.assertEqual((warp_root / "new-registration").read_text(encoding="utf-8"), "new")
                licenses = warp_manager.list_licenses()
                self.assertEqual(len(licenses), 1)
                self.assertTrue(licenses[0]["is_current"])
                self.assertEqual(licenses[0]["status"], "active")
        finally:
            (
                warp_manager.WARP_DATA_DIR,
                warp_manager.DATA_DIR,
                warp_manager.LICENSES_DIR,
                warp_manager.LICENSES_INDEX,
                warp_manager.CURRENT_LICENSE_FILE,
            ) = old_paths

    def test_list_licenses_demotes_stale_active_status_when_not_current(self):
        old_index_path = warp_manager.LICENSES_INDEX
        old_current_file = warp_manager.CURRENT_LICENSE_FILE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                warp_manager.LICENSES_INDEX = root / "licenses" / "index.json"
                warp_manager.CURRENT_LICENSE_FILE = root / "current_license_id"
                warp_manager.set_current_license_id("current-license")
                warp_manager.save_license_index(
                    {
                        "last_id": 2,
                        "licenses": [
                            {"id": "stale-license", "status": "active"},
                            {"id": "current-license", "status": "active"},
                        ],
                    }
                )

                licenses = warp_manager.list_licenses()

                self.assertEqual(licenses[0]["status"], "available")
                self.assertFalse(licenses[0]["is_current"])
                self.assertEqual(licenses[1]["status"], "active")
                self.assertTrue(licenses[1]["is_current"])
        finally:
            warp_manager.LICENSES_INDEX = old_index_path
            warp_manager.CURRENT_LICENSE_FILE = old_current_file


if __name__ == "__main__":
    unittest.main()
