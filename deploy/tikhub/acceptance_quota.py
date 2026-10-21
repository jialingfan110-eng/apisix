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

import sys,time,json,http.client
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent));import gatewayctl as g
inv=g.Inventory('/etc/tikhub-gateway/consumers.json');state=inv.load();a,b=g.TEST_USERS;admin=g.Admin(g.read_env('/etc/tikhub-gateway/stage1.env')['APISIX_ADMIN_KEY'])
def req(who,path):
 c=http.client.HTTPConnection('127.0.0.1',9080,timeout=10);c.request('GET',path,headers={'apikey':state['users'][who]['key']});r=c.getresponse();r.read();c.close();return r.status
for who in (a,b):state['users'][who]['quota']={'count':3,'time_window':60};g.apply_user(admin,who,state['users'][who])
inv.save(state);time.sleep(1)
statuses=[req(a,'/__gateway_acceptance/tikhub/api/v1/quota') for _ in range(4)]
assert statuses==[200,200,200,429],str(statuses)
assert req(b,'/__gateway_acceptance/tikhub/api/v1/quota')==200,'user isolation'
assert req(a,g.AUTH_PATH)==429,'shared routes'
for who in (a,b):state['users'][who]['quota']={'count':100,'time_window':60};g.apply_user(admin,who,state['users'][who])
inv.save(state)
print(json.dumps({'stage':4,'three_allowed_then_429':True,'user_isolation':True,'shared_route_counter':True,'defaults_restored':True}))
