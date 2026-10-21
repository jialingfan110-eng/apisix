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

"""Prepare minimal web credentials and accept an admin password without echo."""
import argparse
import contextlib
import fcntl
import getpass
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import urllib.error
import urllib.request
import warnings

import audit_ops


class SetupError(Exception):
    """A secret-free operator message."""


def require_root():
    if os.geteuid() != 0:
        raise SetupError('Run as root on the server.')


def private_directory(path, create=True):
    path = Path(path)
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SetupError('Private directory must be root-owned mode 0700 and not a symlink.')


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise SetupError('Private file must be root-owned mode 0600 and not a symlink.')
        return source.read()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private(path, data):
    path = Path(path)
    if path.exists() or path.is_symlink():
        read_private(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.admin-private-')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def parse_env(data):
    values = {}
    try:
        lines = data.decode('utf-8').splitlines()
    except UnicodeError:
        raise SetupError('Private environment file is not valid UTF-8.') from None
    for line in lines:
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or key in values or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
            raise SetupError('Private environment file has an invalid or duplicate setting.')
        values[key] = value
    return values


def validate_password(password):
    if len(password) < 12 or '\x00' in password or '\r' in password or '\n' in password or len(password.splitlines()) != 1:
        raise SetupError('Password must contain at least 12 characters and no line breaks or NUL.')
    return password


@contextlib.contextmanager
def setup_lock(directory):
    path = directory / 'admin-setup.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise SetupError('Private lock ownership or permissions are unsafe.')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def verify_reader(password):
    """Verify a previously provisioned account before changing any of its grants."""
    request = urllib.request.Request('http://127.0.0.1:8123/', data=b'SELECT 1',
                                    headers={'X-ClickHouse-User': 'gateway_reader', 'X-ClickHouse-Key': password})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=8) as response:
            if response.read().strip() != b'1':
                raise SetupError('Cannot verify the existing reader credential.')
    except (OSError, urllib.error.HTTPError):
        raise SetupError('Cannot verify the existing reader credential; no account was overwritten.') from None


def initialize(directory, query=None, reader_check=None):
    query = query or audit_ops.query
    reader_check = reader_check or verify_reader
    stage = parse_env(read_private(directory / 'stage1.env'))
    if not stage.get('APISIX_ADMIN_KEY') or not stage.get('CLICKHOUSE_ADMIN_PASSWORD'):
        raise SetupError('Existing gateway infrastructure credentials are incomplete.')
    cli_path = directory / 'web-cli.env'
    existed = cli_path.exists() or cli_path.is_symlink()
    existing = parse_env(read_private(cli_path)) if existed else {}
    if existed and (set(existing) != {'APISIX_ADMIN_KEY', 'APISIX_ADMIN_URL', 'CLICKHOUSE_READ_PASSWORD', 'CLICKHOUSE_URL'}
                    or not re.fullmatch(r'[0-9a-f]{64}', existing.get('CLICKHOUSE_READ_PASSWORD', ''))):
        raise SetupError('Existing web CLI credentials are not a recognized minimal configuration.')
    reader_exists = query(stage, "SELECT count() FROM system.users WHERE name='gateway_reader'").strip()
    if reader_exists not in {'0', '1'}:
        raise SetupError('Cannot determine ClickHouse reader ownership.')
    if reader_exists == '1' and not existed:
        raise SetupError('A reader account already exists without this private credential file; refusing to adopt it.')
    password = existing.get('CLICKHOUSE_READ_PASSWORD') or secrets.token_hex(32)
    if reader_exists == '1':
        reader_check(password)
    desired = {'APISIX_ADMIN_KEY': stage['APISIX_ADMIN_KEY'],
               'APISIX_ADMIN_URL': 'http://apisix:9180/apisix/admin',
               'CLICKHOUSE_READ_PASSWORD': password, 'CLICKHOUSE_URL': 'http://clickhouse:8123'}
    payload = ''.join(key + '=' + value + '\n' for key, value in desired.items()).encode()
    # Save first: a SQL/network failure can be retried with the same credential.
    if existing != desired:
        write_private(cli_path, payload)
    if reader_exists == '0':
        query(stage, "CREATE USER gateway_reader IDENTIFIED WITH sha256_password BY '" + password + "' SETTINGS readonly=1")
    query(stage, 'GRANT SELECT ON tikhub.gateway_access TO gateway_reader')
    # A file created by an older run is never assumed to prove password ownership.
    reader_check(password)


def setup(directory=Path('/etc/tikhub-gateway'), initialize_only=False, password_only=False,
          prompt=None, query=None, reader_check=None):
    require_root()
    directory = Path(directory)
    private_directory(directory)
    if initialize_only and password_only:
        raise SetupError('Choose only one setup mode.')
    with setup_lock(directory):
        admin_path = directory / 'admin.env'
        if admin_path.exists() or admin_path.is_symlink():
            existing_admin = parse_env(read_private(admin_path))
            if set(existing_admin) != {'ADMIN_PASSWORD'}:
                raise SetupError('admin.env must contain ADMIN_PASSWORD only.')
            validate_password(existing_admin['ADMIN_PASSWORD'])
        if not password_only:
            initialize(directory, query=query, reader_check=reader_check)
        if not initialize_only:
            prompt = prompt or getpass.getpass
            # getpass warns and may echo when /dev/tty is unavailable: fail closed.
            if prompt is getpass.getpass and not sys.stdin.isatty():
                raise SetupError('Password setup requires an interactive terminal; no stdin password mode is available.')
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('error', getpass.GetPassWarning)
                    password = validate_password(prompt('管理页密码（至少 12 个字符，不回显）：'))
                    confirmation = prompt('再次输入密码（不回显）：')
            except getpass.GetPassWarning:
                raise SetupError('Cannot disable terminal echo; the existing password was preserved.') from None
            if password != confirmation:
                raise SetupError('Passwords did not match; the existing password was preserved.')
            write_private(admin_path, ('ADMIN_PASSWORD=' + password + '\n').encode('utf-8'))
    print('Private admin configuration prepared; no secrets displayed. Recreate only the admin-web container after password changes.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--initialize-only', action='store_true', help='prepare reader/CLI credentials without prompting for a password')
    mode.add_argument('--password-only', action='store_true', help='set only the web password; skip database provisioning')
    args = parser.parse_args(argv)
    try:
        setup(initialize_only=args.initialize_only, password_only=args.password_only)
    except (SetupError, RuntimeError, OSError) as exc:
        # OSError paths may contain no secrets, but display only a fixed message.
        message = str(exc) if isinstance(exc, (SetupError, RuntimeError)) else 'Cannot safely access the private configuration.'
        raise SystemExit(message) from None


if __name__ == '__main__':
    main()
