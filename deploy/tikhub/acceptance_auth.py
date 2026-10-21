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

import sys,json,time,http.client
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import gatewayctl as g
inv=g.Inventory('/etc/tikhub-gateway/consumer-store/consumers.json')
users=inv.load()['users']; admin=g.Admin(g.read_env('/etc/tikhub-gateway/stage1.env')['APISIX_ADMIN_KEY'])
a,b=g.TEST_USERS; results={}
def request(headers=(),path=g.AUTH_PATH):
 c=http.client.HTTPConnection('127.0.0.1',9080,timeout=10); c.putrequest('GET',path)
 for k,v in headers:c.putheader(k,v)
 c.endheaders(); r=c.getresponse(); body=r.read(); c.close(); return r.status,body

def expect(name,status,headers=(),identity=None,path=g.AUTH_PATH):
 for n in range(60):
  s,body=request(headers,path)
  if s==status and (identity is None or json.loads(body).get('employee_id')==identity):results[name]=True;return
  time.sleep(.2)
 raise RuntimeError(name+' unexpected status '+str(s))
expect('user_a',200,[('apikey',users[a]['key'])],a)
expect('user_b',200,[('apikey',users[b]['key'])],b)
expect('missing_header',401)
expect('wrong_key',401,[('apikey','invalid')])
expect('query_only',401,path=g.AUTH_PATH+'?apikey='+users[a]['key'])
expect('bearer_only',401,[('Authorization','Bearer '+users[a]['key'])])
expect('duplicate_header',401,[('apikey',users[a]['key']),('apikey',users[b]['key'])])
expect('spoofed_identity',200,[('apikey',users[a]['key']),('X-Consumer-Username',b)],a)
users[a]['enabled']=False; inv.save({'schema_version':1,'users':users});g.apply_user(admin,a,users[a])
expect('revoked',401,[('apikey',users[a]['key'])]);expect('unaffected_user',200,[('apikey',users[b]['key'])],b)
old=users[a]['key'];users[a]['enabled']=True;users[a]['key']=g.secrets.token_urlsafe(32);inv.save({'schema_version':1,'users':users});g.apply_user(admin,a,users[a])
expect('rotated_old_rejected',401,[('apikey',old)]);expect('rotated_new_works',200,[('apikey',users[a]['key'])],a)
print(json.dumps({'stage':2,'checks':results,'passed':all(results.values())}))
