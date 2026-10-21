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

"""Receive the TikHub key from a real terminal without echoing or logging it."""

import fcntl
import getpass
import os
from pathlib import Path
import re
import resource
import stat
import sys
import tempfile
import warnings


ENV_FILE = Path("/etc/tikhub-gateway/stage1.env")


def main():
    if os.geteuid() != 0:
        raise RuntimeError("请在服务器上使用 root 或 sudo 执行此工具。")
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise RuntimeError("必须在你亲自操作的交互式 SSH 终端执行，不接受管道或参数传入密钥。")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    parent = ENV_FILE.parent
    if parent.is_symlink() or not parent.is_dir():
        raise RuntimeError("服务器私有配置目录不存在或不安全。")
    info = parent.stat()
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("私有配置目录必须归 root 所有且权限为 0700。")
    lock = os.open(parent / "secrets.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fd = os.open(ENV_FILE, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as source:
            info = os.fstat(source.fileno())
            if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise RuntimeError("现有环境文件必须为 root 所有的 0600 普通文件。")
            original = source.read()
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("请粘贴 TikHub 总 key（输入不会显示），然后按回车：").strip()
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{16,4096}", key):
            raise RuntimeError("密钥格式不符合预期，未保存。请只粘贴完整 key，不要附带引号或其他内容。")
        lines = [line for line in original.splitlines() if not line.startswith("TIKHUB_AUTHORIZATION=")]
        lines.append("TIKHUB_AUTHORIZATION=Bearer " + key)
        fd, temporary = tempfile.mkstemp(prefix=".stage1.env-", dir=parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                target.write("\n".join(lines) + "\n")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, ENV_FILE)
            directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        del key
        print("已安全保存。未显示密钥，未发起任何 TikHub 请求。请回到聊天中回复：已录入。")
    finally:
        os.close(lock)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n已取消录入，未通过聊天传输密钥。", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        # Only our fixed error messages are shown; never display the key or env content.
        if isinstance(exc, RuntimeError):
            print(str(exc), file=sys.stderr)
        else:
            print("录入未完成，请保留终端现场并告知我错误类型：" + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
