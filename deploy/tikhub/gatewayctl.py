#!/usr/bin/env python3
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

"""Manage gateway configuration; sensitive employee/key state stays outside Git."""

import argparse
import contextlib
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid


OWNER = "tikhub-gateway"
TEST_USERS = ("stage2-test-001", "stage2-test-002")
AUTH_ROUTE_ID = "tikhub-acceptance-auth"
AUTH_PATH = "/__gateway_acceptance/auth"
BASE = Path(__file__).resolve().parent

HEADER_GUARD = '''return function(conf, ctx)
    local value = ngx.req.get_headers()["apikey"]
    if type(value) ~= "string" or value == "" or value:find("[,%s]") then
        return 401, {message = "A single apikey header is required"}
    end
    ngx.req.clear_header("X-Consumer-Username")
    ngx.req.clear_header("X-Consumer-Custom-ID")
    ngx.req.clear_header("X-Credential-Identifier")
end'''

AUTH_RESPONSE = '''return function(conf, ctx)
    return 200, {employee_id = ctx.var.consumer_name, environment = "acceptance"}
end'''


class GatewayError(Exception):
    """An operator-facing error whose text must not contain secret material."""


def valid_id(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise GatewayError("员工内部标识必须为 1～64 位字母、数字、下划线或连字符。")
    return value


def read_env(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        if line and not line.startswith("#"):
            key, separator, value = line.partition("=")
            if not separator:
                raise GatewayError("环境文件格式错误。")
            values[key] = value
    return values


def atomic_private_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise GatewayError("私有目录不能是符号链接。")
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".consumers-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Inventory:
    def __init__(self, path):
        self.path = Path(path)

    @contextlib.contextmanager
    def locked(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink():
            raise GatewayError("私有目录不能是符号链接。")
        fd = os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def load(self):
        if not self.path.exists():
            return {"schema_version": 1, "users": {}}
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
                raise GatewayError("员工密钥清单的所有者或权限不安全。")
            value = json.load(source)
        if value.get("schema_version") != 1 or not isinstance(value.get("users"), dict):
            raise GatewayError("员工清单结构不受支持。")
        return value

    def save(self, value):
        atomic_private_json(self.path, value)


class Admin:
    def __init__(self, key):
        if not key:
            raise GatewayError("缺少网关管理密钥。")
        self.key = key
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method, path, value=None, missing_ok=False):
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", path) or ".." in path:
            raise GatewayError("管理资源路径不合法。")
        payload = None if value is None else json.dumps(value).encode()
        request = urllib.request.Request(
            "http://127.0.0.1:9180/apisix/admin" + path,
            data=payload, method=method,
            headers={"X-API-KEY": self.key, "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                data = response.read()
                return json.loads(data) if data else {}
        except urllib.error.HTTPError as exc:
            if missing_ok and exc.code == 404:
                return None
            raise GatewayError("Admin API 返回 HTTP " + str(exc.code) + "；未输出敏感响应。") from None
        except (OSError, ValueError):
            raise GatewayError("无法完成 Admin API 请求；请检查网关状态，随后重新 apply-users。") from None

    def put_managed(self, resource, value):
        existing = self.request("GET", resource, missing_ok=True)
        owned = not existing or existing.get("value", {}).get("labels", {}).get("managed-by") == OWNER
        if existing and resource == "/global_rules/tikhub-audit":
            owned = existing.get("value", {}).get("plugins", {}).get("clickhouse-logger", {}).get("name") == OWNER + "-audit"
        if not owned:
            raise GatewayError("目标资源已由其他流程管理，拒绝覆盖。")
        self.request("PUT", resource, value)

    def delete_managed(self, resource):
        existing = self.request("GET", resource, missing_ok=True)
        if not existing:
            return
        if existing.get("value", {}).get("labels", {}).get("managed-by") != OWNER:
            raise GatewayError("目标资源不是本项目创建的，拒绝删除。")
        self.request("DELETE", resource)


def consumer_document(username, user):
    valid_id(username)
    return {
        "username": username,
        "labels": {"managed-by": OWNER, "kind": user["kind"]},
        "plugins": {"key-auth": {"key": user["key"]},
                    "limit-count": {"count": user["quota"]["count"],
                                    "time_window": user["quota"]["time_window"],
                                    "key_type": "var", "key": "consumer_name", "policy": "local",
                                    "group": "tikhub-" + username + "-" + str(user["quota"]["count"]) + "-" + str(user["quota"]["time_window"]), "rejected_code": 429}},
    }


def apply_user(admin, username, user):
    resource = "/consumers/" + valid_id(username)
    if user["enabled"]:
        admin.put_managed(resource, consumer_document(username, user))
    else:
        admin.delete_managed(resource)


def auth_plugins():
    return {
        "serverless-pre-function": {"phase": "rewrite", "functions": [HEADER_GUARD]},
        "key-auth": {"header": "apikey", "hide_credentials": True},
    }


def auth_route():
    plugins = auth_plugins()
    plugins["consumer-restriction"] = {"whitelist": list(TEST_USERS)}
    plugins["serverless-post-function"] = {"phase": "access", "functions": [AUTH_RESPONSE]}
    return {"name": "tikhub-acceptance-auth", "uri": AUTH_PATH,
            "labels": {"managed-by": OWNER}, "plugins": plugins}


def proxy_route(mock=False):
    prefix = "/__gateway_acceptance/tikhub" if mock else "/tikhub"
    plugins = auth_plugins()
    plugins["proxy-rewrite"] = {
        "regex_uri": ["^" + prefix + "(/api/v1/.*)$", "$1"],
        "headers": {"set": {"Authorization": "Bearer acceptance-only-fake-token" if mock
                              else "$ENV://TIKHUB_AUTHORIZATION"},
                    "remove": ["apikey", "X-Consumer-Username", "X-Consumer-Custom-ID",
                               "X-Credential-Identifier"]}}
    if mock:
        plugins["consumer-restriction"] = {"whitelist": list(TEST_USERS)}
    return {"name": "tikhub-mock" if mock else "tikhub-live", "uri": prefix + "/api/v1/*",
            "labels": {"managed-by": OWNER}, "plugins": plugins,
            "upstream": {"type": "roundrobin", "scheme": "http" if mock else "https",
                         "nodes": {"mock:8080" if mock else "api.tikhub.io:443": 1},
                         "pass_host": "rewrite", "upstream_host": "mock" if mock else "api.tikhub.io",
                         "retries": 0, "timeout": {"connect": 10, "send": 60, "read": 60},
                         **({} if mock else {"tls": {"verify": True}})}}


def audit_document():
    fields = {"request_id": "$apisix_request_id", "employee_id": "$consumer_name",
              "method": "$request_method", "path": "$uri", "status": "$status",
              "upstream_status": "$upstream_status", "duration": "$request_time",
              "upstream_duration": "$upstream_response_time", "timestamp": "$time_iso8601",
              "route_id": "$route_id"}
    return {"plugins": {"clickhouse-logger": {
        "endpoint_addrs": ["http://clickhouse:8123"], "user": "gateway_writer",
        "password": "$ENV://CLICKHOUSE_LOG_PASSWORD", "database": "tikhub",
        "logtable": "gateway_access", "timeout": 2, "name": OWNER + "-audit",
        "log_format": fields, "include_req_body": False, "include_resp_body": False,
        "batch_max_size": 100, "inactive_timeout": 2, "buffer_duration": 5,
        "max_retry_count": 3, "retry_delay": 1,
    }}}


def new_user(display_name, kind="employee", count=100, window=60):
    return {"display_name": display_name, "kind": kind, "enabled": True,
            "key": secrets.token_urlsafe(32), "quota": {"count": count, "time_window": window}}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default="/etc/tikhub-gateway/stage1.env")
    parser.add_argument("--state", default="/etc/tikhub-gateway/consumers.json")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-tests")
    commands.add_parser("apply-users")
    commands.add_parser("users")
    commands.add_parser("apply-mock")
    commands.add_parser("apply-live")
    commands.add_parser("cleanup-tests")
    commands.add_parser("apply-audit")
    commands.add_parser("init-audit")
    health = commands.add_parser("health")
    health.add_argument("--probe", action="store_true")
    commands.add_parser("usage")
    quota = commands.add_parser("user-quota")
    quota.add_argument("username")
    quota.add_argument("count", type=int)
    quota.add_argument("window", type=int)
    add = commands.add_parser("person-add")
    add.add_argument("name")
    add.add_argument("--username")
    for name in ("user-disable", "user-enable", "user-rotate", "user-key"):
        sub = commands.add_parser(name)
        sub.add_argument("username")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    inventory = Inventory(args.state)
    with inventory.locked():
        state = inventory.load()
        users = state["users"]
        if args.command == "users":
            print(json.dumps([{k: u[k] for k in ("display_name", "enabled", "kind", "quota")}
                              | {"username": username} for username, u in users.items()], ensure_ascii=False, indent=2))
            return
        if args.command == "user-key":
            user = users.get(valid_id(args.username))
            if not user or not user["enabled"]:
                raise GatewayError("员工不存在或已停用。")
            if not sys.stdout.isatty():
                raise GatewayError("子 key 仅能在你亲自操作的终端领取，不能输出到管道或自动化日志。")
            print(user["key"])
            return
        env = read_env(args.env_file)
        admin = Admin(env.get("APISIX_ADMIN_KEY"))
        if args.command in ("health", "usage", "init-audit"):
            import audit_ops
            try:
                if args.command == "init-audit":
                    sql = "\n".join(line for line in (BASE / "clickhouse.sql").read_text().splitlines()
                                    if not line.lstrip().startswith("--"))
                    for statement in sql.split(";"):
                        if statement.strip():
                            audit_ops.query(env, statement)
                    print("审计库表初始化完成（已有表保留）。")
                elif args.command == "health":
                    audit_ops.health(env, args.probe)
                else:
                    audit_ops.usage(env, users)
            except RuntimeError as exc:
                raise GatewayError(str(exc)) from None
        elif args.command == "init-tests":
            for username in TEST_USERS:
                if username not in users:
                    users[username] = new_user(username, "acceptance")
                else:
                    users[username]["enabled"] = True
            inventory.save(state)
            for username in TEST_USERS:
                apply_user(admin, username, users[username])
            admin.put_managed("/routes/" + AUTH_ROUTE_ID, auth_route())
            print("隔离测试身份和鉴权验收入口已应用；未输出子 key。")
        elif args.command == "apply-audit":
            if not env.get("CLICKHOUSE_LOG_PASSWORD"):
                raise GatewayError("缺少审计写入凭据。")
            admin.put_managed("/global_rules/tikhub-audit", audit_document())
            admin.request("PUT", "/plugin_metadata/clickhouse-logger", {"max_pending_entries": 8192})
            print("已应用全局异步审计；缓冲上限 8192 条，最多重试 3 次。")
        elif args.command in ("apply-mock", "apply-live"):
            mock = args.command == "apply-mock"
            if not mock and not env.get("TIKHUB_AUTHORIZATION", "").startswith("Bearer "):
                raise GatewayError("请先通过无回显工具录入 TikHub 总 key。")
            admin.put_managed("/routes/tikhub-mock" if mock else "/routes/tikhub-live", proxy_route(mock))
            print("已应用模拟路由。" if mock else "已应用 TikHub 路由；尚未发起真实调用。")
        elif args.command == "cleanup-tests":
            for route in (AUTH_ROUTE_ID, "tikhub-mock"):
                admin.delete_managed("/routes/" + route)
            for username in TEST_USERS:
                admin.delete_managed("/consumers/" + username)
                users.pop(username, None)
            inventory.save(state)
            print("测试路由及测试身份已清理。")
        elif args.command == "person-add":
            if not args.name.strip() or any(ord(c) < 32 for c in args.name):
                raise GatewayError("姓名不能为空或包含控制字符。")
            if any(u["display_name"] == args.name and u["kind"] == "employee" for u in users.values()):
                raise GatewayError("已有同名员工，请先查看内部标识，避免重复建档。")
            username = valid_id(args.username or "emp_" + uuid.uuid4().hex[:16])
            if username in users or username.startswith("stage2-test-"):
                raise GatewayError("员工内部标识已存在或属于测试保留范围。")
            users[username] = new_user(args.name)
            inventory.save(state)
            apply_user(admin, username, users[username])
            print("已建立员工账号：" + username + "；姓名及子 key 仅保存在服务器私有清单。")
        elif args.command == "apply-users":
            for username, user in users.items():
                apply_user(admin, username, user)
            print("已同步员工账号：" + str(len(users)) + " 条；未输出子 key。")
        else:
            username = valid_id(args.username)
            if username not in users:
                raise GatewayError("员工内部标识不存在。")
            if args.command == "user-quota":
                if not (1 <= args.count <= 1000000 and 1 <= args.window <= 86400):
                    raise GatewayError("次数范围 1～1000000，窗口范围 1～86400 秒。")
                users[username]["quota"] = {"count": args.count, "time_window": args.window}
            elif args.command == "user-rotate":
                users[username]["key"] = secrets.token_urlsafe(32)
            else:
                users[username]["enabled"] = args.command == "user-enable"
            inventory.save(state)
            apply_user(admin, username, users[username])
            print("已同步账号状态：" + username + "；未输出子 key。")


if __name__ == "__main__":
    try:
        main()
    except (GatewayError, OSError, ValueError) as exc:
        if isinstance(exc, GatewayError):
            print(str(exc), file=sys.stderr)
        else:
            print("操作未完成：" + type(exc).__name__ + "；未输出敏感数据。", file=sys.stderr)
        sys.exit(1)
