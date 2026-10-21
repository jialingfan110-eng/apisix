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

"""Sanitized web CLI operations. Only explicit creation returns a complete key."""

from datetime import datetime, timedelta, timezone
import json
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo

from gatewayctl import GatewayError, OWNER, apply_user, new_user

TIMEZONE = 'Asia/Shanghai'


def private_url(value, default):
    """URLs are trusted deployment settings, never request parameters."""
    value = value or default
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ValueError
        parsed.port  # Validate even when no explicit connection has been made.
    except ValueError:
        raise RuntimeError('后端连接地址配置不合法。') from None
    return value.rstrip('/')


def today_query(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise RuntimeError('今日用量时间必须带时区。')
    local = now.astimezone(ZoneInfo(TIMEZONE))
    begin = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = begin + timedelta(days=1)
    start_utc = begin.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    end_utc = end.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    # Only clock-derived constants enter SQL. No employee names or request values.
    sql = """SELECT employee_id, uniqExact(request_id) AS calls
FROM tikhub.gateway_access
WHERE received_at >= toDateTime('%s', 'UTC')
  AND received_at < toDateTime('%s', 'UTC')
  AND startsWith(path, '/tikhub/api/v1/')
  AND employee_id NOT IN ('', '-') AND request_id != ''
GROUP BY employee_id ORDER BY calls DESC, employee_id
FORMAT JSONEachRow""" % (start_utc, end_utc)
    return begin.date().isoformat(), sql


def usage_counts(env, now=None):
    password = env.get('CLICKHOUSE_READ_PASSWORD')
    if not password:
        raise RuntimeError('缺少网页只读审计凭据。')
    url = private_url(env.get('CLICKHOUSE_URL'), 'http://127.0.0.1:8123/') + '/'
    day, sql = today_query(now)
    request = urllib.request.Request(url, data=sql.encode(), method='POST', headers={
        'X-ClickHouse-User': 'gateway_reader', 'X-ClickHouse-Key': password,
        'Content-Type': 'text/plain; charset=utf-8',
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=8) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError
        counts = {}
        for line in data.decode().splitlines():
            row = json.loads(line)
            username, calls = row['employee_id'], int(row['calls'])
            if not isinstance(username, str) or calls < 0 or username in counts:
                raise ValueError
            counts[username] = calls
        return day, counts
    except urllib.error.HTTPError as exc:
        raise RuntimeError('ClickHouse 返回 HTTP ' + str(exc.code) + '；未输出敏感响应。') from None
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError('今日用量查询失败；未输出敏感响应。') from None


def creation_time(value):
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (float, int)) and value > 0:
            return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    except (ValueError, OverflowError, OSError):
        pass
    return None


def list_users(state, admin, counts, day, inventory=None):
    response = admin.request('GET', '/consumers')
    entries = response.get('list') if isinstance(response, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError('Admin API Consumer 列表结构不受支持。')
    if response.get('total', len(entries)) != len(entries):
        raise RuntimeError('Admin API Consumer 列表不完整；拒绝展示部分结果。')
    live = {}
    for entry in entries:
        value = entry.get('value') if isinstance(entry, dict) else None
        if not isinstance(value, dict) or not isinstance(value.get('username'), str):
            raise RuntimeError('Admin API Consumer 记录结构不受支持。')
        if value['username'] in live:
            raise RuntimeError('Admin API Consumer 记录重复。')
        live[value['username']] = value
    rows, changed = [], False
    for username in sorted(set(state['users']) | set(live)):
        private, actual = state['users'].get(username), live.get(username)
        owned = bool(actual and actual.get('labels', {}).get('managed-by') == OWNER)
        managed = private is not None and (actual is None or owned)
        actual_key = (actual or {}).get('plugins', {}).get('key-auth', {}).get('key', '')
        if not isinstance(actual_key, str):
            actual_key = ''
        created = creation_time((private or {}).get('created_at'))
        if created is None and actual is not None:
            created = creation_time(actual.get('create_time'))
            if private is not None and created is not None:
                private['created_at'] = created
                changed = True
        if private is None:
            status = 'enabled'
        elif private['enabled']:
            status = 'enabled' if owned and actual_key == private['key'] else 'pending'
        else:
            status = 'disabled' if actual is None else 'pending'
        key = actual_key if actual is not None else (private or {}).get('key', '')
        rows.append({'username': username,
                     'display_name': (private or {}).get('display_name', username),
                     'key_prefix': key[:8], 'created_at': created, 'status': status,
                     'managed': managed, 'today_calls': counts.get(username, 0)})
    if inventory is not None and changed:
        inventory.save(state)
    return {'consumers': rows, 'day': day, 'timezone': TIMEZONE}


def usage_rows(state, counts, day):
    users = state['users']
    rows = [{'username': username, 'display_name': users.get(username, {}).get('display_name', username),
             'calls': counts.get(username, 0)} for username in set(users) | set(counts)]
    rows.sort(key=lambda row: (-row['calls'], row['username']))
    return {'consumers': rows, 'day': day, 'timezone': TIMEZONE}


def employee_name(value):
    # Validate before strip so tabs/newlines cannot be silently accepted.
    if any(unicodedata.category(c).startswith('C') for c in value):
        raise RuntimeError('员工姓名或邮箱不能包含控制字符。')
    value = value.strip()
    if not value or len(value) > 200:
        raise RuntimeError('员工姓名或邮箱长度必须为 1～200 个字符。')
    return value


def create_user(state, inventory, admin, name, request_id):
    name = employee_name(name)
    try:
        request_id = str(uuid.UUID(request_id))
    except (ValueError, AttributeError):
        raise RuntimeError('创建请求标识必须为 UUID。') from None
    users = state['users']
    for username, user in users.items():
        if user.get('web_request_id') == request_id:
            if user['display_name'] != name:
                raise RuntimeError('该创建请求已经用于其他员工。')
            return {'username': username, 'display_name': name, 'already_created': True}
    if any(u['display_name'] == name and u['kind'] == 'employee' for u in users.values()):
        raise RuntimeError('已有同名员工，请先查看内部标识，避免重复建档。')
    username = 'emp_' + uuid.uuid4().hex[:16]
    while username in users:
        username = 'emp_' + uuid.uuid4().hex[:16]
    user = new_user(name)
    user['web_request_id'] = request_id
    users[username] = user
    # Persist request identity before Admin writes. Retrying can never create or
    # reveal another key, including response loss, timeouts and process restarts.
    inventory.save(state)
    try:
        apply_user(admin, username, user)
    except GatewayError:
        raise RuntimeError('员工清单已保存但网关同步失败；请由运维检查待同步账号。') from None
    return {'username': username, 'display_name': name, 'key': user['key']}
