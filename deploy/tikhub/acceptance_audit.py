import sys,time,json,http.client,datetime
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent));import gatewayctl as g;import audit_ops as audit
env=g.read_env('/etc/tikhub-gateway/stage1.env'); inv=g.Inventory('/etc/tikhub-gateway/consumers.json');state=inv.load();users=state['users'];a,b=g.TEST_USERS;admin=g.Admin(env['APISIX_ADMIN_KEY']);checks={}
start=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
def req(path,who=None,body=None):
 c=http.client.HTTPConnection('127.0.0.1',9080,timeout=10);h={} if who is None else {'apikey':users[who]['key']};c.request('POST' if body else 'GET',path,body,h);r=c.getresponse();r.read();c.close();return r.status
assert req(g.AUTH_PATH,a)==200
assert req(g.AUTH_PATH)==401
assert req('/__gateway_acceptance/tikhub/api/v1/failure',b)==503
assert req('/__gateway_acceptance/tikhub/api/v1/audit-echo?secret_query=SECRET_QUERY_MARKER',b,b'SECRET_BODY_MARKER')==200
users[a]['quota']={'count':2,'time_window':60};inv.save(state);g.apply_user(admin,a,users[a]);time.sleep(1)
assert [req(g.AUTH_PATH,a) for _ in range(3)]==[200,200,429]
users[a]['quota']={'count':100,'time_window':60};inv.save(state);g.apply_user(admin,a,users[a])
rows=[]
for _ in range(40):
 rows=[json.loads(l) for l in audit.query(env,"SELECT * FROM tikhub.gateway_access WHERE received_at >= toDateTime('"+start+"','UTC') FORMAT JSONEachRow").splitlines()]
 if {200,401,429,503}<={int(x['status']) for x in rows}:break
 time.sleep(.5)
assert {200,401,429,503}<={int(x['status']) for x in rows},'status coverage'
assert any(x['employee_id']==a and x['status']=='200' for x in rows),'identity attribution'
assert any(x['employee_id']=='' and x['status']=='401' for x in rows),'anonymous attribution'
serialized=json.dumps(rows)
for value in [env['TIKHUB_AUTHORIZATION'].removeprefix('Bearer '),env['APISIX_ADMIN_KEY'],env['CLICKHOUSE_LOG_PASSWORD'],*[x['key'] for x in users.values()],'SECRET_QUERY_MARKER','SECRET_BODY_MARKER']:
 assert value not in serialized,'audit secret or payload exclusion'
try:audit.query(env,'SELECT count() FROM tikhub.gateway_access',writer=True);raise AssertionError('writer can read')
except RuntimeError:checks['writer_select_denied']=True
assert all(x['request_id'] for x in rows),'request IDs'
checks.update({'statuses_200_401_429_503':True,'identity_attribution':True,'secrets_query_body_absent':True,'request_ids_present':True})
print(json.dumps({'stage':5,'rows_observed':len(rows),'checks':checks,'passed':True}))
