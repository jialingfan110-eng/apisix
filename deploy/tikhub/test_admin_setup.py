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

"""Private setup/migration tests use only generated fake credentials."""
import contextlib
import fcntl
import os
import warnings
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
import admin_migrate as migration
import admin_setup as setup


class PrivateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.directory.chmod(0o700)

    def put(self, name, text):
        path = self.directory / name
        path.parent.mkdir(mode=0o700, exist_ok=True)
        setup.write_private(path, text.encode())
        return path

    def state(self, name='consumers.json', suffix='original'):
        value = {'schema_version': 1, 'users': {'emp_a': {'key': 'fake-key-' + suffix, 'enabled': False,
                 'display_name': '测试员工', 'quota': {'count': 100, 'time_window': 60}}}}
        return self.put(name, json.dumps(value, ensure_ascii=False)), value

    def infrastructure(self):
        self.put('stage1.env', 'APISIX_ADMIN_KEY=fake-admin\nCLICKHOUSE_ADMIN_PASSWORD=fake-ch-admin\n'
                 'TIKHUB_AUTHORIZATION=Bearer fake-master-never-copy\nCLICKHOUSE_LOG_PASSWORD=fake-writer-never-copy\n')

    def run_setup(self, **kwargs):
        with patch.object(setup, 'require_root'), contextlib.redirect_stdout(io.StringIO()):
            setup.setup(self.directory, **kwargs)

    def run_migrate(self, **kwargs):
        with patch.object(migration, 'require_root'):
            return migration.migrate(self.directory, **kwargs)

    def initialize(self):
        self.run_setup(initialize_only=True, query=Mock(side_effect=['0', '', '']), reader_check=Mock())

    def test_getpass_echo_fallback_is_refused(self):
        def echo_warning(message):
            warnings.warn('cannot hide input', setup.getpass.GetPassWarning)
            return 'never-reached-password'
        with self.assertRaises(setup.SetupError):
            self.run_setup(password_only=True, prompt=echo_warning)
        self.assertFalse((self.directory / 'admin.env').exists())

    def test_admin_copy_refresh_does_not_rotate_reader(self):
        self.infrastructure()
        self.initialize()
        before = setup.parse_env(setup.read_private(self.directory / 'web-cli.env'))
        self.put('stage1.env', 'APISIX_ADMIN_KEY=fake-admin-new\nCLICKHOUSE_ADMIN_PASSWORD=fake-ch-admin\n')
        self.run_setup(initialize_only=True, query=Mock(side_effect=['1', '']), reader_check=Mock())
        after = setup.parse_env(setup.read_private(self.directory / 'web-cli.env'))
        self.assertEqual(after['APISIX_ADMIN_KEY'], 'fake-admin-new')
        self.assertEqual(after['CLICKHOUSE_READ_PASSWORD'], before['CLICKHOUSE_READ_PASSWORD'])

    def test_migration_holds_both_cli_inventory_locks(self):
        self.state()
        original_write = migration.write_private
        def write_while_checking_locks(path, raw):
            for lock in (self.directory / 'consumers.lock', self.directory / 'consumer-store/consumers.lock'):
                fd = os.open(lock, os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)
            return original_write(path, raw)
        with patch.object(migration, 'write_private', side_effect=write_while_checking_locks):
            self.assertEqual(self.run_migrate()['status'], 'migrated')

    def test_requires_root(self):
        with patch.object(setup.os, 'geteuid', return_value=99), self.assertRaises(setup.SetupError):
            setup.setup(self.directory, password_only=True)

    def test_password_raw_roundtrip_and_no_secret_output(self):
        password = '  fake $foo "quote" # slash\\ end  '
        output = io.StringIO()
        with patch.object(setup, 'require_root'), contextlib.redirect_stdout(output):
            setup.setup(self.directory, password_only=True, prompt=Mock(side_effect=[password, password]))
        raw = setup.read_private(self.directory / 'admin.env')
        self.assertEqual(raw, ('ADMIN_PASSWORD=' + password + '\n').encode())
        self.assertEqual(setup.parse_env(raw), {'ADMIN_PASSWORD': password})
        self.assertNotIn(password, output.getvalue())
        self.assertEqual(stat.S_IMODE((self.directory / 'admin.env').stat().st_mode), 0o600)

    def test_bad_password_preserves_existing(self):
        path = self.put('admin.env', 'ADMIN_PASSWORD=old-password-for-test\n')
        before = path.read_bytes()
        pairs = [('short', 'short'), ('long-enough-but\nlinebreak', 'long-enough-but\nlinebreak'),
                 ('long-enough-aaa', 'long-enough-bbb'), ('long-enough-\x00bad', 'long-enough-\x00bad')]
        for pair in pairs:
            with self.subTest(pair=pair), self.assertRaises(setup.SetupError):
                self.run_setup(password_only=True, prompt=Mock(side_effect=pair))
            self.assertEqual(path.read_bytes(), before)

    def test_minimal_credentials_select_only_sql(self):
        self.infrastructure()
        query = Mock(side_effect=['0', '', ''])
        self.run_setup(initialize_only=True, query=query, reader_check=Mock())
        raw = setup.read_private(self.directory / 'web-cli.env')
        cli = setup.parse_env(raw)
        self.assertEqual(set(cli), {'APISIX_ADMIN_KEY', 'APISIX_ADMIN_URL', 'CLICKHOUSE_READ_PASSWORD', 'CLICKHOUSE_URL'})
        self.assertEqual(len(cli['CLICKHOUSE_READ_PASSWORD']), 64)
        self.assertFalse((self.directory / 'admin.env').exists())
        sql = [call.args[1] for call in query.call_args_list]
        self.assertIn('SETTINGS readonly=1', sql[1])
        self.assertEqual(sql[2], 'GRANT SELECT ON tikhub.gateway_access TO gateway_reader')
        self.assertNotIn('INSERT', ''.join(sql))
        self.assertNotIn(b'fake-master', raw)
        self.assertNotIn(b'fake-writer', raw)
        self.assertEqual(stat.S_IMODE((self.directory / 'web-cli.env').stat().st_mode), 0o600)

    def test_repeat_preserves_reader_and_admin_bytes(self):
        self.infrastructure()
        self.put('admin.env', 'ADMIN_PASSWORD=  fake$"# pass word!  \n')
        self.initialize()
        paths = [self.directory / 'web-cli.env', self.directory / 'admin.env']
        before = [setup.read_private(p) for p in paths]
        query = Mock(side_effect=['1', ''])
        self.run_setup(initialize_only=True, query=query, reader_check=Mock())
        self.assertEqual([setup.read_private(p) for p in paths], before)
        self.assertFalse(any('CREATE USER' in c.args[1] for c in query.call_args_list))

    def test_retry_sql_failure_retains_reader_password(self):
        self.infrastructure()
        with self.assertRaises(RuntimeError):
            self.run_setup(initialize_only=True, query=Mock(side_effect=['0', RuntimeError('safe failure')]), reader_check=Mock())
        before = setup.read_private(self.directory / 'web-cli.env')
        self.initialize()
        self.assertEqual(setup.read_private(self.directory / 'web-cli.env'), before)

    def test_foreign_reader_never_adopted(self):
        self.infrastructure()
        query = Mock(return_value='1')
        with self.assertRaises(setup.SetupError):
            self.run_setup(initialize_only=True, query=query, reader_check=Mock())
        self.assertEqual(query.call_count, 1)
        self.assertFalse((self.directory / 'web-cli.env').exists())

    def test_wrong_existing_reader_password_never_granted(self):
        self.infrastructure()
        self.initialize()
        query = Mock(return_value='1')
        with self.assertRaises(setup.SetupError):
            self.run_setup(initialize_only=True, query=query, reader_check=Mock(side_effect=setup.SetupError('bad credential')))
        self.assertEqual(query.call_count, 1)

    def test_symlink_and_bad_mode_rejected(self):
        original = self.put('original.env', 'ADMIN_PASSWORD=fake-long-password\n')
        (self.directory / 'admin.env').symlink_to(original)
        with self.assertRaises(OSError):
            self.run_setup(password_only=True, prompt=Mock())
        original.chmod(0o644)
        with self.assertRaises(setup.SetupError):
            setup.read_private(original)

    def test_dry_run_changes_no_files(self):
        path, _ = self.state()
        before = set(self.directory.iterdir())
        self.assertEqual(self.run_migrate(dry_run=True)['status'], 'migration_ready')
        self.assertEqual(set(self.directory.iterdir()), before)
        self.assertTrue(path.exists())

    def test_migration_exact_bytes_and_idempotence(self):
        old, value = self.state()
        before = old.read_bytes()
        self.assertTrue(self.run_migrate()['changed'])
        new = self.directory / 'consumer-store/consumers.json'
        self.assertFalse(old.exists())
        self.assertEqual(new.read_bytes(), before)
        self.assertEqual((self.directory / 'consumers.before-admin.json').read_bytes(), before)
        self.assertEqual(json.loads(new.read_bytes()), value)
        self.assertEqual(stat.S_IMODE(new.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(new.parent.stat().st_mode), 0o700)
        self.assertEqual(self.run_migrate()['status'], 'already_migrated')

    def test_rollback_uses_latest_keys(self):
        self.state()
        self.run_migrate()
        new, latest = self.state('consumer-store/consumers.json', 'rotated-after-migration')
        self.assertEqual(self.run_migrate(rollback=True)['status'], 'rolled_back')
        old = self.directory / 'consumers.json'
        self.assertEqual(json.loads(old.read_bytes()), latest)
        self.assertFalse(new.exists())
        self.assertNotEqual(old.read_bytes(), (self.directory / 'consumers.before-admin.json').read_bytes())
        self.assertEqual(self.run_migrate(rollback=True)['status'], 'already_rolled_back')

    def test_both_locations_rejected_in_both_directions(self):
        old, _ = self.state()
        new, _ = self.state('consumer-store/consumers.json', 'different')
        before = (old.read_bytes(), new.read_bytes())
        for rollback in (False, True):
            with self.assertRaises(setup.SetupError):
                self.run_migrate(rollback=rollback)
        self.assertEqual((old.read_bytes(), new.read_bytes()), before)

    def test_bad_inventory_and_parent_rejected(self):
        path = self.put('consumers.json', '{}')
        with self.assertRaises(setup.SetupError):
            self.run_migrate()
        self.state()
        store = self.directory / 'consumer-store'
        store.mkdir(mode=0o700)
        store.chmod(0o755)
        with self.assertRaises(setup.SetupError):
            self.run_migrate()
        self.assertTrue(path.exists())

    def test_failed_write_retains_source(self):
        old, _ = self.state()
        with patch.object(migration, 'write_private', side_effect=OSError('fake disk full')), self.assertRaises(OSError):
            self.run_migrate()
        self.assertTrue(old.exists())
        self.assertTrue((self.directory / 'consumers.before-admin.json').exists())


if __name__ == '__main__':
    unittest.main()
