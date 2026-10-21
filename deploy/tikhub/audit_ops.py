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

"""Local ClickHouse health/usage operations; output never includes credentials."""
import json
import time
import urllib.error
import urllib.request
import uuid


def query(env, sql, writer=False):
    user = 'gateway_writer' if writer else 'gateway_admin'
    password = env['CLICKHOUSE_LOG_PASSWORD' if writer else 'CLICKHOUSE_ADMIN_PASSWORD']
    request = urllib.request.Request('http://127.0.0.1:8123/', data=sql.encode(),
                                     headers={'X-ClickHouse-User': user, 'X-ClickHouse-Key': password})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=8) as response:
            return response.read().decode()
    except urllib.error.HTTPError as exc:
        raise RuntimeError('ClickHouse HTTP ' + str(exc.code) + '; sensitive response suppressed.') from None
    except OSError:
        raise RuntimeError('ClickHouse unavailable; sensitive response suppressed.') from None


def health(env, probe=False):
    result = {'clickhouse': query(env, 'SELECT 1').strip() == '1'}
    if probe:
        path = '/__gateway_health/' + uuid.uuid4().hex
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            opener.open('http://127.0.0.1:9080' + path, timeout=5).close()
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise RuntimeError('Unexpected gateway health response.') from None
        until = time.monotonic() + 20
        delivered = False
        while time.monotonic() < until:
            delivered = int(query(env, "SELECT count() FROM tikhub.gateway_access WHERE path='" + path + "'").strip()) > 0
            if delivered:
                break
            time.sleep(.5)
        result['asynchronous_audit_probe'] = delivered
    print(json.dumps(result))
    if not all(result.values()):
        raise RuntimeError('Audit delivery failed; inspect bounded APISIX logger errors and ClickHouse health.')


def usage(env, users):
    rows = query(env, '''SELECT employee_id, method, path, status, count() AS calls
        FROM tikhub.gateway_access WHERE received_at >= now() - INTERVAL 1 DAY
        GROUP BY employee_id, method, path, status ORDER BY calls DESC LIMIT 200 FORMAT JSONEachRow''')
    for line in rows.splitlines():
        row = json.loads(line)
        row['display_name'] = users.get(row['employee_id'], {}).get('display_name', '')
        print(json.dumps(row, ensure_ascii=False))
