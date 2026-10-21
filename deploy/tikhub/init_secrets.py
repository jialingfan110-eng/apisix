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

"""Initialize private infrastructure keys, preserving all existing values."""
import fcntl
import os
from pathlib import Path
import secrets
import stat
import tempfile


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run as root on the server.')
    path = Path('/etc/tikhub-gateway/stage1.env')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise SystemExit('Private directory must be root-owned, mode 0700, and not a symlink.')
    lock = os.open(path.parent / 'secrets.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lines = []
        if path.exists():
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as source:
                info = os.fstat(source.fileno())
                if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                    raise SystemExit('Private environment file must be root-owned mode 0600.')
                lines = source.read().splitlines()
        existing = {line.partition('=')[0] for line in lines}
        for key, length in [('APISIX_ADMIN_KEY', 32), ('APISIX_DATA_ENCRYPTION_KEY', 16),
                            ('CLICKHOUSE_ADMIN_PASSWORD', 32), ('CLICKHOUSE_LOG_PASSWORD', 32)]:
            if key not in existing:
                lines.append(key + '=' + secrets.token_hex(length))
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.gateway-env-')
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as target:
                target.write('\n'.join(lines) + '\n')
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            os.fsync(fd)
            os.close(fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        print('Infrastructure credentials initialized; existing values preserved, no secrets displayed.')
    finally:
        os.close(lock)


if __name__ == '__main__':
    main()
