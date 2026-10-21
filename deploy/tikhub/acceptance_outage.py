import sys,subprocess,http.client,time,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent));import gatewayctl as g;import audit_ops as audit
env=g.read_env('/etc/tikhub-gateway/stage1.env');u=g.Inventory('/etc/tikhub-gateway/consumers.json').load()['users'][g.TEST_USERS[1]]
base=['docker','compose','--env-file','/etc/tikhub-gateway/stage1.env'];kw={'cwd':str(Path(__file__).resolve().parent),'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL}
subprocess.run(base+['stop','clickhouse'],check=True,timeout=60,**kw)
try:
 c=http.client.HTTPConnection('127.0.0.1',9080,timeout=10);start=time.monotonic();c.request('GET','/__gateway_acceptance/tikhub/api/v1/outage',headers={'apikey':u['key']});r=c.getresponse();r.read();assert r.status==200;latency=time.monotonic()-start;c.close();time.sleep(4)
finally:subprocess.run(base+['up','-d','--pull','never','--wait','--wait-timeout','90','clickhouse'],check=True,timeout=100,**kw)
audit.health(env,probe=True)
p=subprocess.run(['docker','logs','--since','2m','--tail','400','tikhub-gateway-apisix-1'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
errors=sum('failed to connect to host[clickhouse]' in line for line in p.stdout.splitlines())
print(json.dumps({'stage':5,'outage_business_status':200,'latency_ms':round(latency*1000),'logger_connection_errors_observed':errors,'recovered':True}))
