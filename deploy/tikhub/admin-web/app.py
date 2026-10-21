# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements. See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Small session-protected frontend. All gateway access goes through gatewayctl."""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

SESSION_COOKIE = "gateway_session"
LOGIN_COOKIE = "gateway_login"
SESSION_SECONDS = 8 * 3600
LOGIN_SECONDS = 600
RESULT_SECONDS = 300
COMMANDS = frozenset({"web-list", "web-usage", "web-create", "user-disable", "user-enable"})
USERNAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class BackendError(Exception):
    """Safe error boundary: CLI output may contain credentials, never expose it."""


class GatewayBackend:
    def __init__(self, environ):
        self.prefix = [sys.executable, environ.get("GATEWAY_CTL", "/app/gatewayctl.py"),
                       "--env-file", environ.get("GATEWAY_ENV_FILE", "/run/secrets/web-cli.env"),
                       "--state", environ.get("GATEWAY_STATE", "/private/consumers.json")]

    def run(self, command, *args):
        if command not in COMMANDS:
            raise BackendError("Unsupported command")
        try:
            result = subprocess.run(self.prefix + [command, *args], capture_output=True,
                                    text=True, timeout=20, check=False,
                                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                         "PYTHONIOENCODING": "utf-8"})
            if result.returncode:
                raise BackendError("Gateway command failed")
            if command.startswith("web-"):
                value = json.loads(result.stdout)
                if not isinstance(value, dict):
                    raise ValueError("Expected object")
                return value
            return {}
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise BackendError("Gateway command failed") from exc


def create_app(backend=None, environ=None):
    env = dict(os.environ if environ is None else environ)
    password = env.get("ADMIN_PASSWORD", "")
    if not password.strip():
        raise RuntimeError("ADMIN_PASSWORD must be configured and nonempty")
    origin = env.get("ADMIN_ORIGIN", "http://118.196.147.13:8081").rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path:
        raise RuntimeError("ADMIN_ORIGIN must be an HTTP origin")
    allowed_hosts = {x.strip().lower() for x in env.get(
        "ADMIN_ALLOWED_HOSTS", "118.196.147.13,localhost,127.0.0.1").split(",") if x.strip()}
    allowed_hosts.add(parsed.hostname.lower())
    secure = parsed.scheme == "https"
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sessions = {}
    app.state.login_tokens = {}
    app.state.login_attempts = {}
    app.state.results = {}
    app.state.clock = time.monotonic
    backend = backend if backend is not None else GatewayBackend(env)
    app.state.backend = backend
    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    def cleanup():
        now = app.state.clock()
        for mapping in (app.state.sessions, app.state.login_tokens, app.state.results):
            for key, item in list(mapping.items()):
                if item["expires"] <= now:
                    del mapping[key]
        for peer, timestamps in list(app.state.login_attempts.items()):
            fresh = [t for t in timestamps if t > now - 300]
            if fresh:
                app.state.login_attempts[peer] = fresh
            else:
                del app.state.login_attempts[peer]
        return now

    def session(request):
        token = request.cookies.get(SESSION_COOKIE, "")
        item = app.state.sessions.get(token)
        if item is not None and item["expires"] <= app.state.clock():
            app.state.sessions.pop(token, None)
            return None
        return item

    def render(request, template, status=200, **context):
        return templates.TemplateResponse(request=request, name=template, context=context,
                                          status_code=status)

    def error(request, text="暂时无法读取网关数据，请稍后重试或联系运维。", status=502):
        return render(request, "message.html", status=status, message=text, session=session(request))

    def csrf_ok(request, value, item):
        return (request.headers.get("origin", "").rstrip("/") == origin
                and isinstance(value, str) and item is not None
                and hmac.compare_digest(value, item["csrf"]))

    async def call(command, *args):
        try:
            value = await run_in_threadpool(backend.run, command, *args)
            if not isinstance(value, dict):
                raise BackendError()
            return value
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError() from exc

    def collection(value, required):
        rows = value.get("consumers")
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not required <= r.keys() for r in rows):
            raise BackendError()
        if not isinstance(value.get("day"), str) or value.get("timezone") != "Asia/Shanghai":
            raise BackendError()
        return rows

    @app.middleware("http")
    async def guard(request, call_next):
        cleanup()
        try:
            host = urlsplit("//" + request.headers.get("host", "")).hostname
        except ValueError:
            host = None
        if host is None or host.lower() not in allowed_hosts:
            response = HTMLResponse("无效访问地址", status_code=400)
        elif request.url.path == "/favicon.ico":
            # Browser icon requests must not rotate the login form's cookie.
            response = (Response(status_code=204) if request.method in ("GET", "HEAD")
                        else Response(status_code=405, headers={"Allow": "GET, HEAD"}))
        elif request.url.path not in ("/login",) and not request.url.path.startswith("/static/") and not session(request):
            response = RedirectResponse("/login", status_code=303)
        else:
            response = await call_next(request)
            protected = request.url.path != "/login" and not request.url.path.startswith("/static/")
            # Backend I/O yields: a different request may log this session out,
            # or its deadline may pass before this response reaches the browser.
            if protected and not session(request):
                token = request.cookies.get(SESSION_COOKIE, "")
                for result_id, result in list(app.state.results.items()):
                    if result["owner"] == token:
                        del app.state.results[result_id]
                response = RedirectResponse("/login", status_code=303)
                response.delete_cookie(SESSION_COOKIE)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Native form POSTs need their same-origin Origin header for CSRF checks.
        # Cross-origin requests still receive no Referer under this policy.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if session(request):
            return RedirectResponse("/", status_code=303)
        previous = request.cookies.get(LOGIN_COOKIE, "")
        app.state.login_tokens.pop(previous, None)
        token = secrets.token_urlsafe(32)
        item = {"csrf": secrets.token_urlsafe(32), "expires": app.state.clock() + LOGIN_SECONDS}
        # Bound anonymous state, even if a client never sends the cookie back.
        if len(app.state.login_tokens) >= 4096:
            app.state.login_tokens.pop(next(iter(app.state.login_tokens)))
        app.state.login_tokens[token] = item
        response = render(request, "login.html", csrf=item["csrf"], error=None)
        response.set_cookie(LOGIN_COOKIE, token, httponly=True, secure=secure,
                            samesite="strict", max_age=LOGIN_SECONDS)
        return response

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        token = request.cookies.get(LOGIN_COOKIE, "")
        item = app.state.login_tokens.get(token)
        if not csrf_ok(request, form.get("csrf"), item):
            return error(request, "登录页面已过期，请重新打开登录页。", 403)
        peer = request.client.host if request.client else "unknown"
        attempts = app.state.login_attempts.setdefault(peer, [])
        if len(attempts) >= 10:
            return error(request, "登录尝试过多，请五分钟后重试。", 429)
        attempts.append(app.state.clock())
        user = str(form.get("username", ""))
        supplied = str(form.get("password", ""))
        user_ok = hmac.compare_digest(user.encode(), b"admin")
        password_ok = hmac.compare_digest(supplied.encode(), password.encode())
        if not (user_ok and password_ok):
            return render(request, "login.html", status=401, csrf=item["csrf"], error="用户名或密码错误")
        app.state.login_tokens.pop(token, None)
        old_token = request.cookies.get(SESSION_COOKIE, "")
        app.state.sessions.pop(old_token, None)
        session_token = secrets.token_urlsafe(32)
        app.state.sessions[session_token] = {"csrf": secrets.token_urlsafe(32),
            "expires": app.state.clock() + SESSION_SECONDS, "create_ids": set()}
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(SESSION_COOKIE, session_token, httponly=True, secure=secure,
                            samesite="strict", max_age=SESSION_SECONDS)
        response.delete_cookie(LOGIN_COOKIE)
        return response

    @app.post("/logout")
    async def logout(request: Request):
        form = await request.form()
        if not csrf_ok(request, form.get("csrf"), session(request)):
            return error(request, "请求校验失败，请刷新后重试。", 403)
        token = request.cookies.get(SESSION_COOKIE, "")
        app.state.sessions.pop(token, None)
        for result_id, result in list(app.state.results.items()):
            if result["owner"] == token:
                del app.state.results[result_id]
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        try:
            data = await call("web-list")
            rows = collection(data, {"username", "display_name", "key_prefix", "created_at", "status", "managed", "today_calls"})
            if any(not isinstance(r["username"], str) or not isinstance(r["display_name"], str)
                   or not isinstance(r["key_prefix"], str) or not isinstance(r["managed"], bool)
                   or not isinstance(r["today_calls"], int) or isinstance(r["today_calls"], bool)
                   or r["today_calls"] < 0
                   or (r["created_at"] is not None and not isinstance(r["created_at"], str))
                   for r in rows):
                raise BackendError()
            # Defense in depth: never trust a backend to have masked the prefix.
            rows = [{**r, "key_prefix": r["key_prefix"][:8]} for r in rows]
        except BackendError:
            return error(request)
        item = session(request)
        if item is None:
            return RedirectResponse("/login", status_code=303)
        request_id = str(uuid.uuid4())
        if len(item["create_ids"]) >= 100:
            item["create_ids"].pop()
        item["create_ids"].add(request_id)
        return render(request, "index.html", consumers=rows, day=data["day"],
                      session=item, request_id=request_id)

    @app.post("/consumers")
    async def create_consumer(request: Request):
        form = await request.form()
        item = session(request)
        if not csrf_ok(request, form.get("csrf"), item):
            return error(request, "请求校验失败，请刷新后重试。", 403)
        name = str(form.get("name", "")).strip()
        request_id = str(form.get("request_id", ""))
        if not name or len(name) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in name):
            return error(request, "请填写有效姓名或飞书邮箱（最多 128 个字符）。", 400)
        if request_id not in item["create_ids"]:
            return error(request, "新建表单已过期，请返回首页重试。", 400)
        try:
            value = await call("web-create", name, "--request-id", request_id)
            if value.get("already_created"):
                return error(request, "本次创建已经处理，完整 key 不再重复展示；请在列表中查看记录。", 409)
            if not all(isinstance(value.get(k), str) and value[k] for k in ("username", "display_name", "key")):
                raise BackendError()
        except BackendError:
            return error(request, "新建未能完成，请检查是否已存在同名员工；联系运维确认后再操作。")
        if session(request) is None:
            return RedirectResponse("/login", status_code=303)
        result_id = secrets.token_urlsafe(24)
        app.state.results[result_id] = {"owner": request.cookies[SESSION_COOKIE],
            "expires": app.state.clock() + RESULT_SECONDS, "data": value}
        return RedirectResponse("/results/" + result_id, status_code=303)

    @app.get("/results/{result_id}", response_class=HTMLResponse)
    async def result(request: Request, result_id: str):
        value = app.state.results.get(result_id)
        if not value or value["owner"] != request.cookies.get(SESSION_COOKIE):
            return error(request, "完整 key 已展示或已过期，不会再次显示。", 410)
        app.state.results.pop(result_id)
        return render(request, "result.html", result=value["data"], session=session(request))

    @app.post("/consumers/{username}/{action}")
    async def change_consumer(request: Request, username: str, action: str):
        form = await request.form()
        if not csrf_ok(request, form.get("csrf"), session(request)):
            return error(request, "请求校验失败，请刷新后重试。", 403)
        if action not in ("enable", "disable") or not USERNAME.fullmatch(username):
            return error(request, "无效操作。", 400)
        try:
            data = await call("web-list")
            rows = collection(data, {"username", "managed"})
            if session(request) is None:
                return RedirectResponse("/login", status_code=303)
            if not any(row["username"] == username and row["managed"] is True for row in rows):
                return error(request, "该账号只读，不能通过此页面修改。", 403)
            await call("user-" + action, username)
        except BackendError:
            return error(request, "操作未能完成，请刷新列表确认状态。")
        return RedirectResponse("/", status_code=303)

    @app.get("/usage", response_class=HTMLResponse)
    async def usage(request: Request):
        try:
            data = await call("web-usage")
            rows = collection(data, {"username", "display_name", "calls"})
            if any(not isinstance(r["calls"], int) or isinstance(r["calls"], bool) or r["calls"] < 0 for r in rows):
                raise BackendError()
        except BackendError:
            return error(request)
        return render(request, "usage.html", consumers=rows, day=data["day"],
                      maximum=max([r["calls"] for r in rows] + [1]), session=session(request))

    return app
