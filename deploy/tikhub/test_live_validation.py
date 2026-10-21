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


"""Verify request reservations without sending any network traffic."""
import json
from pathlib import Path
import tempfile
import unittest

import gatewayctl as gateway
from validate_live_once import reserve_attempt


class ReservationTests(unittest.TestCase):
    def test_original_reservation_cannot_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, result = reserve_attempt(directory)
            self.assertEqual(result["attempts"], 1)
            with self.assertRaises(FileExistsError):
                reserve_attempt(directory)

    def test_followup_preserves_original_and_cannot_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "real-validation.json"
            prior = {"attempts": 1, "http_status": 502}
            original.write_text(json.dumps(prior))
            ledger, result = reserve_attempt(directory, True)
            self.assertEqual(json.loads(original.read_text()), prior)
            self.assertNotEqual(ledger, original)
            self.assertEqual(result["authorization"], "explicit-followup-2026-10-21")
            with self.assertRaises(FileExistsError):
                reserve_attempt(directory, True)

    def test_followup_refuses_an_unexpected_previous_result(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "real-validation.json"
            original.write_text(json.dumps({"attempts": 1, "http_status": 200}))
            with self.assertRaises(gateway.GatewayError):
                reserve_attempt(directory, True)
            self.assertFalse((Path(directory) / "real-validation-authorized-20261021.json").exists())
