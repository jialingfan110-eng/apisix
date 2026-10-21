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

"""Focused tests for management boundaries; real secrets are never used."""

import contextlib
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import gatewayctl as gateway


class ManagementTests(unittest.TestCase):
    def test_private_store_rejects_unsafe_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = gateway.Inventory(Path(directory) / "state.json")
            inventory.save({"schema_version": 1, "users": {}})
            self.assertEqual(stat.S_IMODE(inventory.path.stat().st_mode), 0o600)
            inventory.path.chmod(0o644)
            with self.assertRaises(gateway.GatewayError):
                inventory.load()

    def test_consumer_data_contains_no_display_name(self):
        user = gateway.new_user("测试姓名")
        doc = gateway.consumer_document("emp_test", user)
        self.assertNotIn("测试姓名", json.dumps(doc, ensure_ascii=False))
        self.assertEqual(doc["username"], "emp_test")
        self.assertEqual(doc["labels"]["managed-by"], gateway.OWNER)

    def test_ids_cannot_select_other_admin_resources(self):
        for value in ("../routes/1", "a/b", "", "a" * 65, "姓名"):
            with self.subTest(value=value), self.assertRaises(gateway.GatewayError):
                gateway.valid_id(value)

    def test_independent_keys_and_default_quota(self):
        a, b = gateway.new_user("A"), gateway.new_user("B")
        self.assertNotEqual(a["key"], b["key"])
        self.assertGreaterEqual(len(a["key"]), 40)
        self.assertEqual(a["quota"], {"count": 100, "time_window": 60})

    def test_auth_route_contains_no_upstream_or_secret(self):
        route = gateway.auth_route()
        self.assertNotIn("upstream", route)
        self.assertEqual(route["plugins"]["serverless-pre-function"]["phase"], "rewrite")
        self.assertTrue(route["plugins"]["key-auth"]["hide_credentials"])
        self.assertEqual(route["plugins"]["consumer-restriction"]["whitelist"], list(gateway.TEST_USERS))

    def test_live_route_uses_environment_secret_and_verified_tls(self):
        route = gateway.proxy_route()
        self.assertEqual(route["plugins"]["proxy-rewrite"]["headers"]["set"]["Authorization"],
                         "$ENV://TIKHUB_AUTHORIZATION")
        self.assertEqual(route["upstream"]["retries"], 0)
        self.assertTrue(route["upstream"]["tls"]["verify"])
        self.assertEqual(route["uri"], "/tikhub/api/v1/*")

    def test_audit_allowlist_has_no_request_secrets_or_bodies(self):
        plugin = gateway.audit_document()["plugins"]["clickhouse-logger"]
        self.assertEqual(set(plugin["log_format"]), {"request_id", "employee_id", "method", "path",
                         "status", "upstream_status", "duration", "upstream_duration", "timestamp", "route_id"})
        self.assertNotIn("$request_uri", json.dumps(plugin))
        self.assertFalse(plugin["include_req_body"])
        self.assertFalse(plugin["include_resp_body"])

    def test_quota_groups_remain_shared_and_allow_configuration_changes(self):
        a = gateway.new_user("test")
        doc = gateway.consumer_document("emp_test", a)
        plugin = doc["plugins"]["limit-count"]
        self.assertEqual(plugin["key"], "consumer_name")
        self.assertEqual(plugin["policy"], "local")
        self.assertEqual(plugin["rejected_code"], 429)
        a["quota"]["count"] = 3
        other = gateway.consumer_document("emp_test", a)["plugins"]["limit-count"]
        self.assertNotEqual(plugin["group"], other["group"])

    def test_refuses_overwriting_foreign_resources(self):
        admin = gateway.Admin("dummy-test-key")
        with patch.object(admin, "request", return_value={"value": {"labels": {"managed-by": "other"}}}) as request:
            with self.assertRaises(gateway.GatewayError):
                admin.put_managed("/routes/test", {})
            self.assertEqual(request.call_count, 1)

    def test_key_cannot_be_printed_to_automation_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            inventory = gateway.Inventory(path)
            user = gateway.new_user("test")
            inventory.save({"schema_version": 1, "users": {"emp_test": user}})
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaises(gateway.GatewayError):
                gateway.main(["--state", str(path), "user-key", "emp_test"])
            self.assertNotIn(user["key"], output.getvalue())


if __name__ == "__main__":
    unittest.main()
