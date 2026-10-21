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

"""Isolated tests: no remote connections or real gateway credentials."""
from concurrent.futures import ThreadPoolExecutor
import re
import threading
import subprocess
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import BackendError, GatewayBackend, LOGIN_COOKIE, SESSION_COOKIE, create_app

ENV = {"ADMIN_PASSWORD": "test-password-only", "ADMIN_ORIGIN": "http://testserver",
       "ADMIN_ALLOWED_HOSTS": "testserver"}
FULL_KEY = "unit-test-only-unique-key-which-must-never-leak"


def hidden(response, name):
    match = re.search(r'name="' + name + r'" value="([^"]+)"', response.text)
    assert match, response.text
    return match.group(1)


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.created = set()
        self.fail = False

    def run(self, command, *args):
        self.calls.append((command, *args))
        if self.fail:
            raise RuntimeError("private upstream credential SHOULD-NOT-APPEAR")
        if command == "web-list":
            return {"day": "2026-10-21", "timezone": "Asia/Shanghai", "consumers": [
                {"username": "emp_disabled", "display_name": "已停用员工", "key_prefix": "abcdefgh",
                 "created_at": None, "status": "disabled", "managed": True, "today_calls": 2},
                {"username": "foreign", "display_name": "<script>alert(1)</script>", "key_prefix": "12345678",
                 "created_at": "2026-10-20", "status": "enabled", "managed": False, "today_calls": 0}]}
        if command == "web-usage":
            return {"day": "2026-10-21", "timezone": "Asia/Shanghai", "consumers": [
                {"username": "emp_disabled", "display_name": "已停用员工", "calls": 2}]}
        if command == "web-create":
            if args[-1] in self.created:
                return {"already_created": True}
            self.created.add(args[-1])
            return {"username": "emp_created", "display_name": args[0], "key": FULL_KEY}
        return {}


class AppTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.app = create_app(self.backend, ENV)
        self.client = TestClient(self.app, follow_redirects=False)

    def login(self, password="test-password-only"):
        page = self.client.get("/login")
        return self.client.post("/login", data={"username": "admin", "password": password,
            "csrf": hidden(page, "csrf")}, headers={"Origin": "http://testserver"})

    def home(self):
        self.assertEqual(self.login().status_code, 303)
        return self.client.get("/")

    def post(self, path, data):
        return self.client.post(path, data=data, headers={"Origin": "http://testserver"})

    def test_password_required(self):
        for env in ({}, {"ADMIN_PASSWORD": ""}, {"ADMIN_PASSWORD": "   "}):
            with self.assertRaises(RuntimeError):
                create_app(self.backend, env)

    def test_anonymous_routes_and_mutations_redirect(self):
        for method, path in [("get", "/"), ("get", "/usage"), ("get", "/results/unknown"),
                             ("get", "/unknown"), ("post", "/consumers"),
                             ("post", "/consumers/emp_disabled/enable"), ("post", "/logout")]:
            response = getattr(self.client, method)(path)
            self.assertEqual(response.status_code, 303, path)
            self.assertEqual(response.headers["location"], "/login")
        self.assertEqual(self.backend.calls, [])

    def test_login_wrong_password_and_success_cookie(self):
        response = self.login("wrong")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(SESSION_COOKIE, self.client.cookies)
        response = self.login()
        self.assertEqual(response.status_code, 303)
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertIn("Max-Age=28800", cookie)
        self.assertNotIn("test-password-only", response.text)

    def test_unicode_password_wrong_username_safe(self):
        client = TestClient(create_app(self.backend, {**ENV, "ADMIN_PASSWORD": "中文密码测试"}), follow_redirects=False)
        page = client.get("/login")
        response = client.post("/login", data={"username": "管理员", "password": "中文密码测试", "csrf": hidden(page, "csrf")}, headers={"Origin": "http://testserver"})
        self.assertEqual(response.status_code, 401)

    def test_login_csrf_origin_and_backoff(self):
        page = self.client.get("/login")
        data = {"username": "admin", "password": "wrong", "csrf": hidden(page, "csrf")}
        self.assertEqual(self.client.post("/login", data=data).status_code, 403)
        self.assertEqual(self.client.post("/login", data=data, headers={"Origin": "http://evil.test"}).status_code, 403)
        for _ in range(10):
            self.assertEqual(self.post("/login", data).status_code, 401)
        # Spoofed forwarded IP cannot reset the limit.
        self.assertEqual(self.client.post("/login", data=data, headers={"Origin": "http://testserver", "X-Forwarded-For": "1.2.3.4"}).status_code, 429)

    def test_host_checked_even_anonymous_and_xff_not_trusted(self):
        self.assertEqual(self.client.get("/", headers={"Host": "evil.test"}).status_code, 400)
        self.assertEqual(self.client.get("/", headers={"X-Forwarded-Host": "evil.test"}).status_code, 303)

    def test_list_disabled_foreign_and_escaping(self):
        page = self.home()
        self.assertEqual(page.status_code, 200)
        self.assertIn("已停用员工", page.text)
        self.assertIn("/consumers/emp_disabled/enable", page.text)
        self.assertNotIn("/consumers/foreign/disable", page.text)
        self.assertIn("只读", page.text)
        self.assertIn("&lt;script&gt;", page.text)
        self.assertNotIn("<script>", page.text)
        self.assertNotIn(FULL_KEY, page.text)
        self.assertEqual(page.headers["cache-control"], "no-store, max-age=0")

    def test_csrf_and_foreign_write_rejected(self):
        page = self.home()
        self.assertEqual(self.post("/consumers/emp_disabled/enable", {"csrf": "forged"}).status_code, 403)
        self.assertEqual(self.post("/consumers/foreign/disable", {"csrf": hidden(page, "csrf")}).status_code, 403)
        self.assertFalse(any(c[0] in ("user-enable", "user-disable") for c in self.backend.calls))

    def test_enable_disable_only_exact_argv(self):
        page = self.home()
        for action in ("enable", "disable"):
            self.assertEqual(self.post("/consumers/emp_disabled/" + action, {"csrf": hidden(page, "csrf")}).status_code, 303)
            self.assertIn(("user-" + action, "emp_disabled"), self.backend.calls)
        self.assertEqual(self.post("/consumers/emp_disabled/rotate", {"csrf": hidden(page, "csrf")}).status_code, 400)

    def test_create_once_and_duplicate_submit(self):
        page = self.home()
        form = {"csrf": hidden(page, "csrf"), "request_id": hidden(page, "request_id"), "name": "张三; $(not-a-shell)@example.com"}
        response = self.post("/consumers", form)
        self.assertEqual(response.status_code, 303)
        location = response.headers["location"]
        self.assertNotIn(FULL_KEY, location)
        self.assertNotIn(FULL_KEY, response.text)
        self.assertIn(("web-create", form["name"], "--request-id", form["request_id"]), self.backend.calls)
        first = self.client.get(location)
        self.assertEqual(first.status_code, 200)
        self.assertIn(FULL_KEY, first.text)
        again = self.client.get(location)
        self.assertEqual(again.status_code, 410)
        self.assertNotIn(FULL_KEY, again.text)
        duplicate = self.post("/consumers", form)
        self.assertEqual(duplicate.status_code, 409)
        self.assertNotIn(FULL_KEY, duplicate.text)

    def test_one_time_result_bound_to_session(self):
        page = self.home()
        response = self.post("/consumers", {"csrf": hidden(page, "csrf"), "request_id": hidden(page, "request_id"), "name": "甲"})
        location = response.headers["location"]
        other = TestClient(self.app, follow_redirects=False)
        page2 = other.get("/login")
        other.post("/login", data={"username": "admin", "password": "test-password-only", "csrf": hidden(page2, "csrf")}, headers={"Origin": "http://testserver"})
        self.assertEqual(other.get(location).status_code, 410)
        self.assertEqual(self.client.get(location).status_code, 200)

    def test_bad_name_and_request_id(self):
        page = self.home()
        form = {"csrf": hidden(page, "csrf"), "request_id": hidden(page, "request_id"), "name": "line\nbreak"}
        self.assertEqual(self.post("/consumers", form).status_code, 400)
        form.update(name="甲", request_id="unknown")
        self.assertEqual(self.post("/consumers", form).status_code, 400)
        self.assertFalse(any(c[0] == "web-create" for c in self.backend.calls))

    def test_logout_expiry_forgery(self):
        page = self.home()
        self.assertEqual(self.post("/logout", {"csrf": hidden(page, "csrf")}).status_code, 303)
        self.assertEqual(self.client.get("/").status_code, 303)
        self.home()
        now = self.app.state.clock()
        self.app.state.clock = lambda: now + 8 * 3600 + 1
        self.assertEqual(self.client.get("/").status_code, 303)
        self.client.cookies.set(SESSION_COOKIE, "forged")
        self.assertEqual(self.client.get("/").status_code, 303)

    def test_backend_failure_safe_not_empty(self):
        self.login()
        self.backend.fail = True
        response = self.client.get("/")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("SHOULD-NOT-APPEAR", response.text)
        self.assertNotIn("暂无员工", response.text)
        self.assertEqual(self.client.get("/usage").status_code, 502)

    def test_malformed_json_object_not_empty_list(self):
        self.login()
        with patch.object(self.backend, "run", return_value={"consumers": "bad"}):
            self.assertEqual(self.client.get("/").status_code, 502)
            self.assertEqual(self.client.get("/usage").status_code, 502)

    def test_usage_chart(self):
        self.login()
        page = self.client.get("/usage")
        self.assertEqual(page.status_code, 200)
        self.assertIn('progress max="2" value="2"', page.text)
        self.assertIn("2026-10-21", page.text)

    def test_result_expires_without_second_reveal(self):
        page = self.home()
        response = self.post("/consumers", {"csrf": hidden(page, "csrf"),
            "request_id": hidden(page, "request_id"), "name": "甲"})
        now = self.app.state.clock()
        self.app.state.clock = lambda: now + 301
        self.assertEqual(self.client.get(response.headers["location"]).status_code, 410)

    def test_incorrect_backend_prefix_is_truncated(self):
        self.login()
        data = self.backend.run("web-list")
        data["consumers"][0]["key_prefix"] = FULL_KEY
        with patch.object(self.backend, "run", return_value=data):
            page = self.client.get("/")
            self.assertEqual(page.status_code, 200)
            self.assertIn(FULL_KEY[:8], page.text)
            self.assertNotIn(FULL_KEY, page.text)

    def test_failed_create_is_sanitized(self):
        page = self.home()
        self.backend.fail = True
        response = self.post("/consumers", {"csrf": hidden(page, "csrf"),
            "request_id": hidden(page, "request_id"), "name": "甲"})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("SHOULD-NOT-APPEAR", response.text)
        self.assertEqual(self.app.state.results, {})

    def test_logout_or_expiry_while_backend_is_running(self):
        for endpoint, command in (("/", "web-list"), ("/usage", "web-usage"),
                                  ("/consumers", "web-create"),
                                  ("/consumers/emp_disabled/enable", "web-list")):
            for invalidation in ("logout", "expiry"):
                with self.subTest(endpoint=endpoint, invalidation=invalidation):
                    self.setUp()
                    page = self.home()
                    csrf = hidden(page, "csrf")
                    form = {"csrf": csrf, "request_id": hidden(page, "request_id"), "name": "并发测试"}
                    waiting, release = threading.Event(), threading.Event()
                    original = self.backend.run
                    def blocked(cmd, *args):
                        if cmd == command:
                            waiting.set()
                            if not release.wait(5):
                                raise RuntimeError("test barrier timed out")
                        return original(cmd, *args)
                    other = TestClient(self.app, follow_redirects=False)
                    other.cookies.update(self.client.cookies)
                    def request():
                        if endpoint.startswith("/consumers"):
                            return other.post(endpoint, data=form, headers={"Origin": "http://testserver"})
                        return other.get(endpoint)
                    with patch.object(self.backend, "run", side_effect=blocked):
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            future = pool.submit(request)
                            try:
                                self.assertTrue(waiting.wait(3))
                                if invalidation == "logout":
                                    self.assertEqual(self.post("/logout", {"csrf": csrf}).status_code, 303)
                                else:
                                    now = self.app.state.clock()
                                    self.app.state.clock = lambda: now + 8 * 3600 + 1
                            finally:
                                release.set()
                            response = future.result(timeout=5)
                    self.assertEqual(response.status_code, 303)
                    self.assertEqual(response.headers["location"], "/login")
                    self.assertNotIn(FULL_KEY, response.text)
                    self.assertNotIn("已停用员工", response.text)
                    self.assertEqual(self.app.state.results, {})
                    self.assertFalse(any(c[0] == "user-enable" for c in self.backend.calls))

    def test_gateway_backend_rejects_command_and_bad_json(self):
        backend = GatewayBackend({})
        with self.assertRaises(BackendError):
            backend.run("user-key", "emp_disabled")
        fake = subprocess.CompletedProcess([], 0, stdout="private non JSON", stderr="")
        with patch("app.subprocess.run", return_value=fake) as run:
            with self.assertRaises(BackendError):
                backend.run("web-list")
            args, kwargs = run.call_args
            self.assertIsInstance(args[0], list)
            self.assertNotIn("shell", kwargs)


if __name__ == "__main__":
    unittest.main()
