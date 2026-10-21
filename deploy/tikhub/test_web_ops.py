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

"""Web interfaces tested without live APIs or private credentials."""
from datetime import datetime, timezone
import contextlib
import http.server
import subprocess
import sys
import threading
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import gatewayctl as g
import web_ops as w


class FakeAdmin:
    def __init__(self, entries=None, fail=False):
        self.entries, self.fail = entries or [], fail
        self.applied = []
    def request(self, *args, **kwargs):
        if self.fail: raise g.GatewayError('Admin unavailable')
        return {'list': self.entries, 'total': len(self.entries)}
    def put_managed(self, resource, value):
        if self.fail: raise g.GatewayError('Admin unavailable')
        self.applied.append((resource, value))


def entry(username, user, owned=True):
    doc = g.consumer_document(username, user)
    if not owned: doc['labels'] = {'managed-by': 'other'}
    doc['create_time'] = 1600000000
    return {'value': doc}


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.inventory = g.Inventory(Path(self.temp.name) / 'state.json')
        self.state = {'schema_version': 1, 'users': {}}
        self.inventory.save(self.state)
    def create(self, name='张三', request_id='fdeeb828-6101-49eb-8896-90d85f5a6b18', admin=None):
        return w.create_user(self.state, self.inventory, admin or FakeAdmin(), name, request_id)
    def test_once_only_creation(self):
        a, b = self.create(), self.create()
        self.assertIn('key', a)
        self.assertNotIn('key', b)
        self.assertTrue(b['already_created'])
        self.assertEqual(a['username'], b['username'])
        self.assertEqual(len(self.state['users']), 1)
    def test_failed_create_has_no_repeat_or_secret_reveal(self):
        with self.assertRaises(RuntimeError): self.create(admin=FakeAdmin(fail=True))
        duplicate = self.create()
        self.assertTrue(duplicate['already_created'])
        self.assertNotIn('key', duplicate)
        self.assertEqual(len(self.state['users']), 1)
    def test_conflicting_duplicate_request_rejected(self):
        self.create()
        with self.assertRaises(RuntimeError): self.create(name='李四')
    def test_invalid_names_and_request_ids(self):
        for name in ('', ' ', 'line\nfeed', 'bad\x7f', 'A' * 201):
            with self.subTest(name=repr(name)), self.assertRaises(RuntimeError): self.create(name=name)
        with self.assertRaises(RuntimeError): self.create(request_id='../bad')
    def test_name_trim_and_duplicate(self):
        self.create(name='  person@example.com  ')
        with self.assertRaises(RuntimeError):
            self.create(name='person@example.com', request_id='0b587ed8-cf28-42b5-bf60-8c393922c5fb')
    def test_merged_status_and_secret_masking(self):
        active, disabled, pending, foreign = [g.new_user(n) for n in ('活跃', '停用', '待同步', '外部')]
        disabled['enabled'] = False
        for user in (active, disabled, pending): user.pop('created_at')
        self.state['users'] = {'a': active, 'd': disabled, 'p': pending}
        result = w.list_users(self.state, FakeAdmin([entry('a', active), entry('f', foreign, False)]), {'a': 4}, '2026-10-21')
        rows = {r['username']: r for r in result['consumers']}
        self.assertEqual([rows[k]['status'] for k in ('a', 'd', 'p')], ['enabled', 'disabled', 'pending'])
        self.assertFalse(rows['f']['managed'])
        self.assertEqual(rows['a']['key_prefix'], active['key'][:8])
        self.assertEqual(rows['a']['today_calls'], 4)
        self.assertNotIn(active['key'], json.dumps(result))
        self.assertEqual(rows['a']['created_at'], '2020-09-13T12:26:40Z')
        self.assertIsNone(rows['d']['created_at'])
    def test_fixed_creation_timestamp(self):
        user = g.new_user('A')
        user['created_at'] = '2021-01-01T00:00:00Z'
        self.state['users']['a'] = user
        result = w.list_users(self.state, FakeAdmin([entry('a', user)]), {}, '2026-10-21')
        self.assertEqual(result['consumers'][0]['created_at'], user['created_at'])
    def test_legacy_timestamp_persisted(self):
        user = g.new_user('A')
        user.pop('created_at')
        self.state['users']['a'] = user
        w.list_users(self.state, FakeAdmin([entry('a', user)]), {}, '2026-10-21', self.inventory)
        self.assertEqual(self.inventory.load()['users']['a']['created_at'], '2020-09-13T12:26:40Z')
    def test_admin_failure_not_empty_list(self):
        with self.assertRaises(g.GatewayError): w.list_users(self.state, FakeAdmin(fail=True), {}, '2026-10-21')
    def test_midnight_calendar_bounds_and_fixed_sql(self):
        day, sql = w.today_query(datetime(2026, 10, 20, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(day, '2026-10-21')
        for part in ('2026-10-20 16:00:00', '2026-10-21 16:00:00', 'uniqExact(request_id)', '/tikhub/api/v1/', 'received_at <'):
            self.assertIn(part, sql)
    def test_usage_preserves_historical_identifiers(self):
        self.state['users']['a'] = g.new_user('A')
        result = w.usage_rows(self.state, {'a': 3, 'gone': 2}, '2026-10-21')
        self.assertEqual(result['consumers'], [{'username': 'a', 'display_name': 'A', 'calls': 3}, {'username': 'gone', 'display_name': 'gone', 'calls': 2}])
    def test_reader_credentials_and_url(self):
        captured = {}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit=None): return b'{"employee_id":"a","calls":3}\n'
        class Opener:
            def open(self, req, timeout):
                captured['request'] = req
                return Response()
        with patch('web_ops.urllib.request.build_opener', return_value=Opener()):
            _, calls = w.usage_counts({'CLICKHOUSE_READ_PASSWORD': 'dummy', 'CLICKHOUSE_URL': 'http://clickhouse:8123/'})
        req = captured['request']
        self.assertEqual(req.full_url, 'http://clickhouse:8123/')
        self.assertEqual(req.get_header('X-clickhouse-user'), 'gateway_reader')
        self.assertEqual(calls, {'a': 3})
    def test_custom_admin_url(self):
        admin = g.Admin('dummy', 'http://apisix:9180/apisix/admin')
        self.assertEqual(admin.base_url, 'http://apisix:9180/apisix/admin')
    def test_cli_creation_json_and_idempotency(self):
        env = Path(self.temp.name) / 'cli.env'
        env.write_text('APISIX_ADMIN_KEY=dummy\n')
        argv = ['--state', str(self.inventory.path), '--env-file', str(env), 'web-create', 'A', '--request-id', 'fdeeb828-6101-49eb-8896-90d85f5a6b18']
        a, b = io.StringIO(), io.StringIO()
        with patch.object(g, 'Admin', return_value=FakeAdmin()):
            with contextlib.redirect_stdout(a): g.main(argv)
            with contextlib.redirect_stdout(b): g.main(argv)
        self.assertIn('key', json.loads(a.getvalue()))
        self.assertNotIn('key', json.loads(b.getvalue()))

    def test_create_idempotency_persists_across_process_state_reload(self):
        first = self.create()
        self.state = self.inventory.load()
        duplicate = self.create()
        self.assertEqual(first['username'], duplicate['username'])
        self.assertNotIn('key', duplicate)

    def test_partial_admin_list_is_error(self):
        admin = FakeAdmin()
        admin.request = lambda *args: {'list': [], 'total': 1}
        with self.assertRaises(RuntimeError): w.list_users(self.state, admin, {}, '2026-10-21')

    def test_pending_key_mismatch_disabled_still_present_and_foreign_collision(self):
        a, b, c = [g.new_user(n) for n in ('A', 'B', 'C')]
        b['enabled'] = False
        self.state['users'] = {'a': a, 'b': b, 'c': c}
        wrong = dict(a, key='different-dummy-key')
        result = w.list_users(self.state, FakeAdmin([entry('a', wrong), entry('b', b), entry('c', c, False)]), {}, '2026-10-21')
        self.assertEqual([r['status'] for r in result['consumers']], ['pending', 'pending', 'pending'])
        self.assertFalse(result['consumers'][2]['managed'])

    def test_failed_clickhouse_is_error_not_zero_usage(self):
        with patch('web_ops.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = OSError('test-source-private-body')
            with self.assertRaises(RuntimeError) as raised:
                w.usage_counts({'CLICKHOUSE_READ_PASSWORD': 'dummy'})
        self.assertNotIn('test-source-private-body', str(raised.exception))

    def test_invalid_urls_rejected_without_echo(self):
        for url in ('http://user:dummy@apisix:9180/apisix/admin', 'file:///tmp/dummy', 'http://apisix:bad'):
            with self.subTest(url=url), self.assertRaises(g.GatewayError) as raised:
                g.Admin('dummy', url)
            self.assertNotIn('dummy', str(raised.exception))

    def test_legacy_disable_preserves_original_timestamp(self):
        user = g.new_user('Legacy')
        user.pop('created_at')
        self.state['users']['legacy'] = user
        self.inventory.save(self.state)
        env = Path(self.temp.name) / 'cli.env'
        env.write_text('APISIX_ADMIN_KEY=dummy\n')
        admin = FakeAdmin()
        admin.request = lambda *args, **kwargs: {'value': {'create_time': 1600000000}}
        admin.delete_managed = lambda *args: None
        argv = ['--state', str(self.inventory.path), '--env-file', str(env)]
        with patch.object(g, 'Admin', return_value=admin), contextlib.redirect_stdout(io.StringIO()):
            g.main(argv + ['user-disable', 'legacy'])
            g.main(argv + ['user-enable', 'legacy'])
        actual = self.inventory.load()['users']['legacy']
        self.assertTrue(actual['enabled'])
        self.assertEqual(actual['created_at'], '2020-09-13T12:26:40Z')
        self.assertEqual(actual['key'], user['key'])

    def test_actual_script_failed_creation_is_sanitized_and_retry_idempotent(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404)
                self.end_headers()
            def do_PUT(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b'SECRET-DUMMY-RESPONSE')
            def log_message(self, *args): pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        env = Path(self.temp.name) / 'cli.env'
        env.write_text('APISIX_ADMIN_KEY=dummy\nAPISIX_ADMIN_URL=http://127.0.0.1:' + str(server.server_port) + '/apisix/admin\n')
        argv = [sys.executable, str(Path(g.__file__)), '--state', str(self.inventory.path), '--env-file', str(env), 'web-create', 'Actual script', '--request-id', 'fdeeb828-6101-49eb-8896-90d85f5a6b18']
        first = subprocess.run(argv, text=True, capture_output=True, timeout=10)
        self.assertEqual(first.returncode, 1)
        self.assertEqual(first.stdout, '')
        self.assertNotIn('SECRET-DUMMY-RESPONSE', first.stderr)
        self.assertNotIn('Traceback', first.stderr)
        self.assertIn('已保存', first.stderr)
        second = subprocess.run(argv, text=True, capture_output=True, timeout=10)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(json.loads(second.stdout)['already_created'])
        self.assertNotIn('key', json.loads(second.stdout))


if __name__ == '__main__': unittest.main()
