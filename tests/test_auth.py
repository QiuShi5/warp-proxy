import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.auth import (
    AuthConfig,
    build_basic_auth_header,
    create_session_token,
    get_auth_config,
    install_auth,
    verify_session_token,
)


def make_test_app():
    app = FastAPI()
    install_auth(app)

    @app.get("/")
    async def index():
        return {"ok": True}

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/secret")
    async def secret():
        return {"secret": True}

    return app


class AuthTests(unittest.TestCase):
    def test_session_token_round_trip_and_rejects_tampering(self):
        config = AuthConfig(
            username="admin",
            password="secret",
            session_secret="signing-secret",
            session_ttl=300,
            secure_cookie=False,
        )

        token = create_session_token("admin", config)

        self.assertEqual(verify_session_token(token, config), "admin")
        self.assertIsNone(verify_session_token(f"{token}x", config))

    def test_session_token_rejects_expired_payload(self):
        config = AuthConfig(
            username="admin",
            password="secret",
            session_secret="signing-secret",
            session_ttl=-1,
            secure_cookie=False,
        )

        token = create_session_token("admin", config)

        self.assertIsNone(verify_session_token(token, config))

    def test_management_api_requires_login_but_healthcheck_is_public(self):
        env = {
            "WEB_USER": "admin",
            "WEB_PASS": "secret",
            "WEB_SESSION_SECRET": "signing-secret",
        }

        with patch.dict(os.environ, env, clear=False):
            client = TestClient(make_test_app())

            self.assertEqual(client.get("/api/health").status_code, 200)
            self.assertEqual(client.get("/api/secret").status_code, 401)

            login = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "secret"},
            )
            self.assertEqual(login.status_code, 200)
            self.assertIn("warp_proxy_session", login.headers.get("set-cookie", ""))
            self.assertEqual(client.get("/api/secret").status_code, 200)

            logout = client.post("/api/auth/logout")
            self.assertEqual(logout.status_code, 200)
            self.assertEqual(client.get("/api/secret").status_code, 401)

    def test_basic_auth_allows_internal_api_calls(self):
        env = {
            "WEB_USER": "admin",
            "WEB_PASS": "secret",
            "WEB_SESSION_SECRET": "signing-secret",
        }

        with patch.dict(os.environ, env, clear=False):
            client = TestClient(make_test_app())
            response = client.get(
                "/api/secret",
                headers=build_basic_auth_header("admin", "secret"),
            )

        self.assertEqual(response.status_code, 200)

    def test_wrong_login_is_rejected(self):
        env = {
            "WEB_USER": "admin",
            "WEB_PASS": "secret",
            "WEB_SESSION_SECRET": "signing-secret",
        }

        with patch.dict(os.environ, env, clear=False):
            client = TestClient(make_test_app())
            response = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "wrong"},
            )

        self.assertEqual(response.status_code, 401)

    def test_default_web_password_placeholder_does_not_override_proxy_password(self):
        env = {
            "WEB_USER": "admin",
            "WEB_PASS": "change_this_web_password",
            "WEB_SESSION_SECRET": "change_this_session_secret",
            "PROXY_PASS": "proxy-secret",
        }

        with patch.dict(os.environ, env, clear=False):
            config = get_auth_config()

        self.assertEqual(config.password, "proxy-secret")
        self.assertEqual(config.session_secret, "proxy-secret")


if __name__ == "__main__":
    unittest.main()
