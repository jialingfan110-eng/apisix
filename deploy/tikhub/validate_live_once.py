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

"""One authorized, non-retrying validation of the officially listed free user-info endpoint."""
import argparse
import datetime
import http.client
import json
import os
from pathlib import Path
import sys

import gatewayctl as gateway


def reserve_attempt(directory, followup=False):
    """Reserve one request, preserving the original and separately authorized ledgers."""
    original = Path(directory) / 'real-validation.json'
    ledger = original
    if followup:
        fd = os.open(original, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            prior = json.load(source)
        if prior.get('http_status') != 502 or prior.get('attempts') != 1:
            raise gateway.GatewayError('The authorized follow-up requires the original failed attempt.')
        ledger = Path(directory) / 'real-validation-authorized-20261021.json'
    result = {'endpoint': '/api/v1/tikhub/user/get_user_info', 'attempts': 1,
              'listed_price_usd': '0.0000', 'authorized_ceiling_usd': '0.50',
              'authorization': 'explicit-followup-2026-10-21' if followup else 'original',
              'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'state': 'attempted'}
    if followup:
        result['previous_attempt_ledger'] = original.name
    fd = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as target:
        json.dump(result, target)
        target.flush()
        os.fsync(target.fileno())
    return ledger, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authorized-followup-2026-10-21', action='store_true', dest='followup',
                        help='Use the single extra request explicitly authorized on 2026-10-21.')
    args = parser.parse_args()
    state = gateway.Inventory('/etc/tikhub-gateway/consumers.json').load()
    employees = [value for value in state['users'].values() if value['kind'] == 'employee' and value['enabled']]
    if not employees:
        raise gateway.GatewayError('No enabled employee available.')
    ledger, result = reserve_attempt('/etc/tikhub-gateway', args.followup)
    connection = http.client.HTTPConnection('127.0.0.1', 9080, timeout=80)
    try:
        connection.request('GET', '/tikhub' + result['endpoint'], headers={'apikey': employees[0]['key']})
        response = connection.getresponse()
        body = response.read(1024 * 1024)
        env = gateway.read_env('/etc/tikhub-gateway/stage1.env')
        forbidden = [env['TIKHUB_AUTHORIZATION'].removeprefix('Bearer '), employees[0]['key']]
        result['http_status'] = response.status
        result['credentials_absent_from_response'] = not any(v.encode() in body for v in forbidden)
        try:
            data = json.loads(body)
            result['business_code'] = data.get('code')
            result['user_info_received'] = isinstance(data.get('user_data'), dict)
        except (ValueError, AttributeError):
            result['user_info_received'] = False
        result['passed'] = response.status == 200 and result.get('business_code') == 200 and result['user_info_received'] and result['credentials_absent_from_response']
        result['state'] = 'completed'
    except (OSError, http.client.HTTPException):
        result['state'] = 'failed_or_unknown'
        result['passed'] = False
    finally:
        connection.close()
        gateway.atomic_private_json(ledger, result)
    print(json.dumps(result))
    return 0 if result.get('passed') else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except FileExistsError:
        print('The authorized attempt has already been reserved; no request sent.', file=sys.stderr)
        sys.exit(1)
    except (gateway.GatewayError, OSError):
        print('Validation unavailable; no sensitive output.', file=sys.stderr)
        sys.exit(1)
