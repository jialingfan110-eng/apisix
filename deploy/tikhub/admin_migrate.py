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

"""Move the private employee inventory without changing existing child keys."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import stat

from admin_setup import (SetupError, private_directory, read_private, require_root,
                         sync_directory, write_private)


def exists(path):
    return path.exists() or path.is_symlink()


def inventory(path):
    data = read_private(path)
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        raise SetupError('Employee inventory is not valid JSON.') from None
    if not isinstance(value, dict) or value.get('schema_version') != 1 or not isinstance(value.get('users'), dict):
        raise SetupError('Employee inventory has an unsupported structure.')
    return data, value


@contextlib.contextmanager
def inventory_lock(path):
    fd = os.open(path.with_suffix('.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise SetupError('Inventory lock ownership or permissions are unsafe.')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def migrate(directory=Path('/etc/tikhub-gateway'), dry_run=False, rollback=False):
    require_root()
    directory = Path(directory)
    private_directory(directory, create=False)
    old = directory / 'consumers.json'
    new = directory / 'consumer-store' / 'consumers.json'
    backup = directory / 'consumers.before-admin.json'
    if exists(new.parent):
        private_directory(new.parent, create=False)
    old_exists, new_exists = exists(old), exists(new)
    if old_exists and new_exists:
        raise SetupError('Both inventory locations exist; refusing an ambiguous migration. Preserve both files for operator review.')
    if not old_exists and not new_exists:
        raise SetupError('No employee inventory exists; refusing to create an empty replacement.')
    source = new if rollback else old
    destination = old if rollback else new
    if exists(destination):
        inventory(destination)
        return {'status': 'already_rolled_back' if rollback else 'already_migrated', 'changed': False}
    raw, original = inventory(source)
    if exists(backup):
        inventory(backup)  # Never overwrite an original backup, including on rollback.
    if dry_run:
        return {'status': 'rollback_ready' if rollback else 'migration_ready', 'changed': False,
                'users': len(original['users'])}
    private_directory(new.parent)
    # Both CLI versions use these same lock paths. Pause all web/CLI mutations
    # during migration and switch all callers to the new path before resuming.
    with inventory_lock(old), inventory_lock(new):
        if exists(destination):
            raise SetupError('Inventory changed during migration; no file was overwritten.')
        raw, original = inventory(source)
        if not rollback and not exists(backup):
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, 'wb') as target:
                    target.write(raw)
                    target.flush()
                    os.fsync(target.fileno())
                sync_directory(directory)
            except BaseException:
                # Leave a possibly incomplete backup for explicit operator review.
                raise
        write_private(destination, raw)
        if inventory(destination)[1] != original:
            raise SetupError('Inventory verification failed; the source was retained.')
        os.unlink(source)
        sync_directory(source.parent)
    return {'status': 'rolled_back' if rollback else 'migrated', 'changed': True, 'users': len(original['users'])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='read-only preflight; does not lock or create files')
    parser.add_argument('--rollback', action='store_true', help='move the current new inventory back, retaining all intervening changes')
    args = parser.parse_args(argv)
    try:
        result = migrate(dry_run=args.dry_run, rollback=args.rollback)
    except (SetupError, OSError) as exc:
        raise SystemExit(str(exc) if isinstance(exc, SetupError) else 'Cannot safely access the private inventory; source retained.') from None
    print(json.dumps(result))


if __name__ == '__main__':
    main()
