-- Licensed to the Apache Software Foundation (ASF) under one or more
-- contributor license agreements. See the NOTICE file distributed with
-- this work for additional information regarding copyright ownership.
-- The ASF licenses this file to You under the Apache License, Version 2.0
-- (the "License"); you may not use this file except in compliance with
-- the License. You may obtain a copy of the License at
--
--     http://www.apache.org/licenses/LICENSE-2.0
--
-- Unless required by applicable law or agreed to in writing, software
-- distributed under the License is distributed on an "AS IS" BASIS,
-- WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
-- See the License for the specific language governing permissions and
-- limitations under the License.

CREATE DATABASE IF NOT EXISTS tikhub;
CREATE TABLE IF NOT EXISTS tikhub.gateway_access
(
    request_id String DEFAULT '',
    employee_id String DEFAULT '',
    method LowCardinality(String) DEFAULT '',
    path String DEFAULT '',
    status String DEFAULT '',
    upstream_status String DEFAULT '',
    duration String DEFAULT '',
    upstream_duration String DEFAULT '',
    timestamp String DEFAULT '',
    route_id String DEFAULT '',
    received_at DateTime DEFAULT now()
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(received_at)
ORDER BY (employee_id, received_at, request_id)
TTL received_at + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192;
