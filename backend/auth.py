"""
Authentication helpers for the web management UI and API.

Browser users authenticate with an HTTP-only session cookie. Internal manager
to node API calls may use HTTP Basic auth with the same credentials.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field


SESSION_COOKIE = "warp_proxy_session"
DEFAULT_WEB_USER = "admin"
DEFAULT_WEB_PASS = "change_this_web_password"
DEFAULT_SESSION_SECRET = "change_this_session_secret"


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=512)


@dataclass(frozen=True)
class AuthConfig:
    username: str
    password: str
    session_secret: str
    session_ttl: int
    secure_cookie: bool


def _env_first(*names: str, default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_auth_config() -> AuthConfig:
    username = _env_first("WEB_USER", "PROXY_USER", default=DEFAULT_WEB_USER)
    web_pass = os.getenv("WEB_PASS") or ""
    proxy_pass = os.getenv("PROXY_PASS") or ""
    if web_pass and web_pass != DEFAULT_WEB_PASS:
        password = web_pass
    elif proxy_pass:
        password = proxy_pass
    else:
        password = web_pass or DEFAULT_WEB_PASS

    configured_secret = os.getenv("WEB_SESSION_SECRET") or ""
    if configured_secret and configured_secret != DEFAULT_SESSION_SECRET:
        session_secret = configured_secret
    elif password != DEFAULT_WEB_PASS:
        session_secret = password
    else:
        session_secret = configured_secret or DEFAULT_SESSION_SECRET

    return AuthConfig(
        username=username,
        password=password,
        session_secret=session_secret,
        session_ttl=_env_int("WEB_SESSION_TTL", default=86400, minimum=300),
        secure_cookie=_env_bool("WEB_COOKIE_SECURE", default=False),
    )


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _sign(value: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), value.encode("ascii"), hashlib.sha256)
    return _b64encode(digest.digest())


def create_session_token(username: str, config: Optional[AuthConfig] = None) -> str:
    cfg = config or get_auth_config()
    payload = {
        "u": username,
        "exp": int(time.time()) + cfg.session_ttl,
        "n": secrets.token_urlsafe(18),
    }
    encoded_payload = _b64encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    return f"{encoded_payload}.{_sign(encoded_payload, cfg.session_secret)}"


def verify_session_token(token: str, config: Optional[AuthConfig] = None) -> Optional[str]:
    cfg = config or get_auth_config()
    try:
        encoded_payload, signature = token.split(".", 1)
        expected = _sign(encoded_payload, cfg.session_secret)
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_b64decode(encoded_payload).decode("utf-8"))
    except Exception:
        return None

    if payload.get("u") != cfg.username:
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    return cfg.username


def verify_credentials(
    username: str,
    password: str,
    config: Optional[AuthConfig] = None,
) -> bool:
    cfg = config or get_auth_config()
    return hmac.compare_digest(username, cfg.username) and hmac.compare_digest(
        password,
        cfg.password,
    )


def build_basic_auth_header(username: str, password: str) -> dict:
    raw = f"{username}:{password}".encode("utf-8")
    return {"Authorization": f"Basic {base64.b64encode(raw).decode('ascii')}"}


def get_node_basic_auth_header() -> dict:
    cfg = get_auth_config()
    username = os.getenv("NODE_WEB_USER") or cfg.username
    password = os.getenv("NODE_WEB_PASS") or cfg.password
    return build_basic_auth_header(username, password)


def _basic_auth_username(request: Request, config: AuthConfig) -> Optional[str]:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value:
        return None
    try:
        decoded = base64.b64decode(value).decode("utf-8")
        username, separator, password = decoded.partition(":")
    except Exception:
        return None
    if not separator:
        return None
    return username if verify_credentials(username, password, config) else None


def authenticated_username(request: Request, config: Optional[AuthConfig] = None) -> Optional[str]:
    cfg = config or get_auth_config()
    basic_username = _basic_auth_username(request, cfg)
    if basic_username:
        return basic_username
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return verify_session_token(token, cfg)


def _set_session_cookie(response: Response, username: str, config: AuthConfig) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(username, config),
        max_age=config.session_ttl,
        httponly=True,
        samesite="lax",
        secure=config.secure_cookie,
    )


def _clear_session_cookie(response: Response, config: AuthConfig) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        samesite="lax",
        secure=config.secure_cookie,
    )


def _is_public_path(path: str) -> bool:
    return path in {
        "/api/health",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/me",
        "/login",
        "/favicon.ico",
    }


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录 - warp-proxy 管理面板</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #07111d;
    --panel: rgba(14, 28, 44, .96);
    --line: rgba(135, 158, 184, .28);
    --text: #eef5ff;
    --muted: #95a8bd;
    --blue: #2684ff;
    --red: #ef5b5b;
  }
  * { box-sizing: border-box; }
  body {
    min-height: 100vh;
    margin: 0;
    display: grid;
    place-items: center;
    padding: 24px;
    background: linear-gradient(145deg, #05101c 0%, #0b1725 58%, #08121f 100%);
    color: var(--text);
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  main {
    width: min(420px, 100%);
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--panel);
    box-shadow: 0 18px 48px rgba(0, 0, 0, .26);
    overflow: hidden;
  }
  header { padding: 26px 26px 14px; }
  h1 { margin: 0; font-size: 22px; line-height: 1.25; }
  p { margin: 8px 0 0; color: var(--muted); font-size: 14px; }
  form { display: grid; gap: 14px; padding: 18px 26px 26px; }
  label { display: grid; gap: 7px; color: var(--muted); font-size: 13px; }
  input {
    height: 42px;
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: 0 12px;
    outline: 0;
    background: rgba(6, 17, 30, .82);
    color: var(--text);
    font: inherit;
  }
  input:focus { border-color: rgba(38, 132, 255, .8); }
  button {
    height: 42px;
    border: 0;
    border-radius: 7px;
    color: #fff;
    background: var(--blue);
    font: inherit;
    font-weight: 750;
    cursor: pointer;
  }
  button:disabled { opacity: .6; cursor: not-allowed; }
  #error { min-height: 18px; color: var(--red); font-size: 13px; }
</style>
</head>
<body>
<main>
  <header>
    <h1>WARP Proxy Manager</h1>
    <p>登录后访问管理面板和管理 API。</p>
  </header>
  <form id="loginForm">
    <label>用户名<input id="username" name="username" autocomplete="username" required autofocus></label>
    <label>密码<input id="password" name="password" type="password" autocomplete="current-password" required></label>
    <div id="error" role="alert"></div>
    <button id="submitBtn" type="submit">登录</button>
  </form>
</main>
<script>
const form = document.getElementById('loginForm');
const errorBox = document.getElementById('error');
const submitBtn = document.getElementById('submitBtn');

form.addEventListener('submit', async event => {
  event.preventDefault();
  errorBox.textContent = '';
  submitBtn.disabled = true;
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: document.getElementById('username').value,
        password: document.getElementById('password').value,
      }),
    });
    if (!res.ok) {
      errorBox.textContent = '用户名或密码错误';
      return;
    }
    window.location.href = '/';
  } catch (error) {
    errorBox.textContent = '登录请求失败';
  } finally {
    submitBtn.disabled = false;
  }
});
</script>
</body>
</html>
"""


def install_auth(app: FastAPI) -> None:
    @app.middleware("http")
    async def require_management_login(
        request: Request,
        call_next: Callable,
    ) -> Response:
        path = request.url.path
        username = authenticated_username(request)

        if path == "/login" and username:
            return RedirectResponse("/", status_code=303)

        if _is_public_path(path):
            return await call_next(request)

        if username:
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)

        return RedirectResponse("/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        return HTMLResponse(LOGIN_PAGE)

    @app.post("/api/auth/login")
    async def login(payload: LoginPayload):
        cfg = get_auth_config()
        if not verify_credentials(payload.username, payload.password, cfg):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        response = JSONResponse({"authenticated": True, "username": cfg.username})
        _set_session_cookie(response, cfg.username, cfg)
        return response

    @app.post("/api/auth/logout")
    async def logout():
        cfg = get_auth_config()
        response = JSONResponse({"authenticated": False})
        _clear_session_cookie(response, cfg)
        return response

    @app.get("/api/auth/me")
    async def me(request: Request):
        username = authenticated_username(request)
        return {"authenticated": bool(username), "username": username}
