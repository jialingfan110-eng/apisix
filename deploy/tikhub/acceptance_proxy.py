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

import sys,json,time,http.client,base64
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import gatewayctl as g
u=g.Inventory('/etc/tikhub-gateway/consumer-store/consumers.json').load()['users'][g.TEST_USERS[0]];checks={}
def req(method,path,body=b'',headers=None):
 c=http.client.HTTPConnection('127.0.0.1',9080,timeout=15)
 h={'apikey':u['key'],'Content-Type':'application/octet-stream','Authorization':'Bearer supplied-fake','X-Consumer-Username':'spoof'}
 h.update(headers or {});c.request(method,'/__gateway_acceptance/tikhub'+path,body,h);r=c.getresponse();data=r.read();c.close();return r.status,data
for i in range(50):
 status,data=req('GET','/api/v1/echo')
 if status==200:break
 time.sleep(.2)
assert status==200, 'mock route status '+str(status)
query='?a=1&a=2&empty=&plus=a+b&percent=%2f%2F&utf=%E4%B8%AD&apikey=business-value'
for method in ['GET','POST','PUT','PATCH','DELETE','OPTIONS']:
 body=b'\x00\xffbinary\n'+bytes(range(128)) if method in ['POST','PUT','PATCH','DELETE'] else b''
 status,data=req(method,'/api/v1/echo'+query,body);v=json.loads(data)
 assert status==200 and v['method']==method and v['uri']=='/api/v1/echo'+query and base64.b64decode(v['body_base64'])==body, method+' preservation'
 assert v['fake_authorization_ok'] and v['apikey_absent'] and v['consumer_header_absent'],method+' credentials'
 checks[method]=True
status,data=req('GET','/api/v1/raw');assert status==418 and data==b'raw\x00body\xff';checks['status_and_binary_response']=True
status,data=req('GET','/api/v1/failure');assert status==503;checks['upstream_503']=True
print(json.dumps({'stage':3,'checks':checks,'passed':True}))
