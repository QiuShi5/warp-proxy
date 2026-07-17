import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class PackagingTests(unittest.TestCase):
    def test_dockerfile_provides_entrypoint_runtime_commands(self):
        dockerfile = read_text("Dockerfile")

        for package in ("curl", "procps", "dbus", "python3", "python3-pip"):
            self.assertRegex(dockerfile, rf"\b{re.escape(package)}\b")

        self.assertIn("cloudflare-warp", dockerfile)
        self.assertIn("/usr/local/bin/gost", dockerfile)
        self.assertIn("ln -sf /usr/bin/python3 /usr/local/bin/python", dockerfile)
        self.assertIn("ln -sf /usr/bin/pip3 /usr/local/bin/pip", dockerfile)

    def test_entrypoint_does_not_call_unresolved_python_binary(self):
        entrypoint = read_text("entrypoint.sh")

        self.assertIn('PYTHON_BIN="${PYTHON_BIN:-}"', entrypoint)
        self.assertIn('command -v python3', entrypoint)
        self.assertIn('exec "$PYTHON_BIN" -m uvicorn backend.cluster_app:app', entrypoint)
        self.assertIn('nohup "$PYTHON_BIN" -m uvicorn backend.app:app', entrypoint)
        self.assertIsNone(re.search(r"\b(?:exec|nohup)\s+python\s+-m\s+uvicorn", entrypoint))

    def test_entrypoint_fails_before_ready_when_backend_is_unhealthy(self):
        entrypoint = read_text("entrypoint.sh")

        health_check = entrypoint.index("Web management backend is healthy.")
        ready_banner = entrypoint.index("=== warp-proxy ready ===")
        self.assertLess(health_check, ready_banner)
        self.assertIn("ERROR: Web management backend exited during startup.", entrypoint)
        self.assertIn("ERROR: Web management backend did not become healthy.", entrypoint)
        self.assertIn("cat /data/backend.log", entrypoint)

    def test_web_ui_has_single_static_dashboard_source(self):
        static_dir = ROOT / "backend" / "static"

        self.assertTrue((static_dir / "dashboard.html").is_file())
        self.assertFalse((static_dir / "index.html").exists())
        self.assertFalse((static_dir / "cluster.html").exists())
        self.assertIn('DASHBOARD_HTML = STATIC_DIR / "dashboard.html"', read_text("backend/app.py"))
        self.assertIn('DASHBOARD_HTML = STATIC_DIR / "dashboard.html"', read_text("backend/cluster_app.py"))

    def test_management_auth_is_wired_for_single_and_cluster_apps(self):
        single_app = read_text("backend/app.py")
        cluster_app = read_text("backend/cluster_app.py")
        cluster_manager = read_text("backend/cluster_manager.py")

        self.assertIn("from .auth import install_auth", single_app)
        self.assertIn("from .auth import install_auth", cluster_app)
        self.assertIn("install_auth(app)", single_app)
        self.assertIn("install_auth(app)", cluster_app)
        self.assertIn("get_node_basic_auth_header", cluster_manager)
        self.assertIn("headers=self.auth_headers", cluster_manager)

    def test_dashboard_uses_current_binding_for_active_license(self):
        dashboard = read_text("backend/static/dashboard.html")

        self.assertIn("const active = Boolean(license.is_current);", dashboard)
        self.assertNotIn("license.is_current || license.status === 'active'", dashboard)

    def test_dashboard_text_is_not_mojibake(self):
        dashboard = read_text("backend/static/dashboard.html")
        auth = read_text("backend/auth.py")

        for text in ("warp-proxy 管理面板", "总览", "节点管理", "License 管理", "负载均衡", "节点设置", "日志"):
            self.assertIn(text, dashboard)
        self.assertIn("登录后访问管理面板和管理 API", auth)
        self.assertNotIn("?" * 4, dashboard)
        self.assertNotIn("?" * 4, auth)

    def test_dashboard_keeps_license_generation_out_of_node_management(self):
        dashboard = read_text("backend/static/dashboard.html")

        self.assertNotIn("nodeGenerateBtn", dashboard)
        self.assertNotIn("generateAllBtn", dashboard)
        self.assertIn("licensesGenerateBtn", dashboard)

    def test_dashboard_groups_balancer_targets_by_node(self):
        dashboard = read_text("backend/static/dashboard.html")

        self.assertIn("function groupedBalancerTargets", dashboard)
        self.assertIn("balance-node-head", dashboard)
        self.assertIn("balance-methods", dashboard)
        self.assertIn("group.methods.map", dashboard)
        self.assertNotIn("list.innerHTML = targets.map(target", dashboard)

    def test_dashboard_handles_management_auth_session(self):
        dashboard = read_text("backend/static/dashboard.html")

        self.assertIn("logoutBtn", dashboard)
        self.assertIn("/api/auth/me", dashboard)
        self.assertIn("/api/auth/logout", dashboard)
        self.assertIn("res.status === 401", dashboard)
        self.assertIn("window.location.href = '/login'", dashboard)

    def test_compose_healthchecks_target_existing_api(self):
        for compose_file in (
            "docker-compose.yml",
            "docker-compose.cluster.yml",
            "docker-compose.local.yml",
        ):
            compose = read_text(compose_file)
            self.assertIn("http://127.0.0.1:8000/api/health", compose)
            self.assertNotIn("index.html", compose)
            self.assertNotIn("cluster.html", compose)

    def test_compose_files_define_management_auth_environment(self):
        for compose_file in (
            "docker-compose.yml",
            "docker-compose.cluster.yml",
            "docker-compose.local.yml",
        ):
            compose = read_text(compose_file)
            self.assertIn("WEB_USER=${WEB_USER:-admin}", compose)
            self.assertIn("WEB_PASS=${WEB_PASS:-change_this_web_password}", compose)
            self.assertIn(
                "WEB_SESSION_SECRET=${WEB_SESSION_SECRET:-change_this_session_secret}",
                compose,
            )


if __name__ == "__main__":
    unittest.main()
