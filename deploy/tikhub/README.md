# TikHub 统一网关

APISIX 接收 `apikey: <个人子 key>`，去掉 `/tikhub` 前缀，以服务器注入的 `Authorization: Bearer <总 key>` 调用 `https://api.tikhub.io`。方法、Query、请求体、上游状态和响应体透传。ClickHouse 异步记录调用审计。

本期包含个人建档、停用、轮换、按人计数限流及审计；飞书多维表格接入和自服务门户留待后续。员工姓名保存在服务器私有清单，APISIX 使用稳定内部 ID；以后改名无需重建 key，同名人员需运维指定不同内部 ID。

## 文件与边界

| 文件 | 用途 |
| --- | --- |
| `docker-compose.yml` | APISIX、etcd、ClickHouse 和可选模拟服务编排；APISIX 启动 YAML 内嵌于 Compose |
| `gatewayctl.py` | Consumer、路由、限额、审计及密钥领取命令 |
| `init_secrets.py` | 初始化基础设施密钥，重复运行保留已有值 |
| `receive_tikhub_key.py` | 在交互终端无回显接收 TikHub 总 key |
| `audit_ops.py` | ClickHouse 查询、用量及异步探针 |
| `clickhouse.sql` / `clickhouse*.xml` | 表结构、30 天保留期、插入专用账号和资源配置 |
| `mock.conf` / `acceptance_*.py` | 隔离模拟及阶段验收，不调用 TikHub |
| `validate_live_once.py` | 仅一次真实账户信息接口验收，持久化防重试记录 |
| `test_gatewayctl.py` / `test_live_validation.py` | 管理边界、敏感配置及一次性请求记录测试 |

真实密钥和员工清单分别在 `/etc/tikhub-gateway/stage1.env`、`/etc/tikhub-gateway/consumer-store/consumers.json`（已有安装先运行下面的迁移命令），目录 0700、文件 0600，均不进入 Git。环境文件名沿用阶段一。服务器 root / Docker 管理员属于可信管理边界，可读取容器环境。

固定镜像：APISIX 3.19.0、etcd 3.6.15、ClickHouse 25.8 LTS，Compose 同时固定 amd64 镜像摘要。Ubuntu 22.04/24.04 amd64、Docker Compose **≥2.30.0**（需要 `configs.content`）、Python **≥3.9**；本次实际服务器为 Ubuntu 24.04、Docker 29.1.3、Compose 2.40.3。

资源上限：APISIX 768 MB、etcd 512 MB、ClickHouse 2 GB。建议至少 4 GB 可用内存并留足业务余量。网段默认 `10.203.90.0/24`；部署前检查现有 Docker 网络、路由与 9080/9180/8123 端口。可通过环境文件覆盖 `TIKHUB_NETWORK_SUBNET` / `TIKHUB_NETWORK_GATEWAY`。

## 阶段一：启动基础设施

以下在服务器仓库 `deploy/tikhub/` 执行，需 root 或 sudo。新环境先获取本仓库：

```bash
git clone https://github.com/jialingfan110-eng/apisix.git /opt/tikhub-gateway
cd /opt/tikhub-gateway/deploy/tikhub
python3 init_secrets.py
docker compose --env-file /etc/tikhub-gateway/stage1.env config --quiet
docker compose --env-file /etc/tikhub-gateway/stage1.env pull
docker compose --env-file /etc/tikhub-gateway/stage1.env up -d --wait --wait-timeout 180
docker compose --env-file /etc/tikhub-gateway/stage1.env ps
```

预期三项服务 healthy；初次空 etcd 尚无业务路由，9080 返回 404。Admin 9180 仅宿主机 127.0.0.1，缺管理 key 返回 401；etcd 不发布端口；ClickHouse 8123 仅 127.0.0.1 和私有容器网络。Dashboard 经 SSH 转发访问，管理 API 仍需独立 Admin key。

```bash
ssh -N -L 19180:127.0.0.1:9180 root@服务器IP
# 本机浏览器打开 http://127.0.0.1:19180/ui/
```

总 key 尚未录入时，APISIX 可以启动，但 `apply-live` 拒绝创建真实路由。不要输出完整 `docker compose config`、容器 environment、环境文件或启用 shell `set -x`。

### 本次离线镜像缓存

服务器无法直连 Docker Hub，公开镜像从官方仓库在本机下载，逐 blob 验证 SHA256 后通过 SSH 导入；未修改 Docker 全局配置或使用第三方镜像源。缓存位于 `/var/cache/tikhub-gateway/`：

| 文件 | SHA256 |
| --- | --- |
| `apisix-3.19.0-amd64.oci.tar` | `73a6246607da3668f54cd381dd4abf4e4e4ba781c295a7817cf33bd15a7de860` |
| `clickhouse-25.8-amd64.oci.tar` | `9663158d91f5802e01e676e439b04db7920fb69ac9541a0ace803922f232abe6` |

恢复镜像先用 `sha256sum` 核对上述值，再 `docker image load --input <缓存绝对路径>`。使用 `docker image inspect <Compose 中带摘要的完整镜像引用> --format '{{.Id}}'` 确认能按摘要找到镜像，随后 `up -d --pull never --wait`。新服务器需自行准备相同摘要的官方镜像；缓存不随 Git 提交。

## 阶段二：员工与子 key

```bash
python3 gatewayctl.py person-add '同事飞书姓名'
python3 gatewayctl.py users
```

创建后输出稳定 `emp_...` ID；姓名和随机子 key 仅写入私有清单。默认每人每 60 秒 100 次，可在下一节调整。重复姓名默认拒绝，确有同名人员应使用区分后的显示名并指定 `--username emp_独立标识`。姓名不是认证凭据。

在本人操作的 SSH 终端领取 key（自动化、重定向或管道输出被拒绝）：

```bash
python3 gatewayctl.py user-key emp_替换为实际ID
python3 gatewayctl.py user-disable emp_替换为实际ID
python3 gatewayctl.py user-enable emp_替换为实际ID
python3 gatewayctl.py user-rotate emp_替换为实际ID
```

轮换后重新领取，旧 key 失效。禁用会删除该 Consumer，私有清单保留记录便于恢复。配置经 etcd 异步传到工作进程，需等待数秒后验收。若管理 API 调用失败，清单先保留期望状态；恢复后执行 `python3 gatewayctl.py apply-users`。

鉴权只接受单个、非空 `apikey` header；Query-only、Bearer-only、错误 key、重复 header 都拒绝。Query 中本来就有的 `apikey` 参数作为业务参数保留，不参与认证。客户端伪造的员工身份头被清除。

## 阶段三：安全录入与透传

在交互式 SSH 终端执行，提示后粘贴总 key 并回车，屏幕不回显，不进入 shell 历史：

```bash
python3 receive_tikhub_key.py
docker compose --env-file /etc/tikhub-gateway/stage1.env up -d --pull never --force-recreate --wait --wait-timeout 180 apisix
python3 gatewayctl.py apply-live
```

`apply-live` 不发起上游请求。总 key 通过 Compose environment 注入，路由只存 `$ENV://TIKHUB_AUTHORIZATION` 引用。NGINX 启动时全局启用上游证书校验、载入系统 CA，校验深度为 5；上游 HTTPS 使用正确 Host/SNI；业务重试次数为 **0**，避免付费调用被自动重复。再次录入总 key 后需重建 APISIX 使新环境生效。

同事请求地址：`http://服务器IP:9080/tikhub/api/v1/...`。使用 `apikey` header，具体方法、参数和请求体按 TikHub 官方接口文档。HTTP 不加密网络传输，这是本期明确选择；9080 供业务调用，9180/8123 保持本机绑定。

`validate_live_once.py` 是本次授权验收工具，只调用 `/api/v1/tikhub/user/get_user_info` 一次。2026-10-20 官方 API 市场列价 $0.0000/请求，授权上限 $0.50。执行前再次核对官方价格；该脚本不适合作为定时健康检查。它在发请求前创建私有 `real-validation.json`，失败或超时也不自动重试，禁止删除记录来绕过次数限制；响应正文、邮箱、余额均不打印。2026-10-21 用户另行授权 TLS 修复后的 1 次重验，使用显式参数 `--authorized-followup-2026-10-21`，额外记录保存在 `real-validation-authorized-20261021.json`，原始记录保持不变。此次额外授权已经使用并通过；重复执行该参数也不会再发请求。

## 阶段四：按人计数限流

```bash
python3 gatewayctl.py user-quota emp_替换为实际ID 100 60
```

即每 60 秒最多 100 次，超额返回 429。相同员工跨路由共享，其他员工独立，`policy=local`，不依赖 Redis。次数按尝试计算，上游失败也计入；它不是美元预算或真实账单。改限额会切换计数组，重启进程会清空本地计数；切回仍未过期的旧组可能沿用旧计数。避免用反复更新策略当作配额重置工具。

## 阶段五：异步审计

```bash
python3 gatewayctl.py init-audit
python3 gatewayctl.py apply-audit
python3 gatewayctl.py health --probe
python3 gatewayctl.py usage
```

全局审计包含匹配及未匹配请求、401、429和上游失败。字段限定为请求 ID、内部员工 ID、方法、路径（无 Query）、网关及上游状态、耗时、时间、路由 ID；鉴权失败没有员工 ID。请求头、Query、请求体、响应体及姓名均不采集。`usage` 在服务器查询最近 24 小时，再用私有清单映射显示名；它输出调用次数，不能视为 TikHub 实际金额。

ClickHouse 命名卷保存日志，表 `tikhub.gateway_access`，按接收时间保留 30 天（TTL 合并异步清理，非逐秒删除）。`gateway_writer` 只有 INSERT 权限，只允许本项目网段；管理查询使用独立 `gateway_admin`。密码由外部 env 提供。

日志批次最多 100 条，空闲约 2 秒或缓冲约 5 秒发送，HTTP 超时 2 秒，最多重试 3 次、重试间隔 1 秒，内存待处理上限 8192 条。ClickHouse 故障不阻断业务；重试耗尽、缓冲满、进程崩溃或强制重启可能丢日志，网络不确定性下重试可能重复，不能承诺零丢失或恰好一次。聚合可按 request_id 辅助去重。

### 健康与告警接入

`health --probe` 不请求 TikHub：向网关发送无凭据、随机路径的 404 请求，然后最多等待 20 秒确认异步落库。成功退出 0，ClickHouse 不可用或未落库退出非 0。可由现有监控系统每分钟执行并对连续失败告警；本次不另行注册通知账号或新定时任务。

排障限量读取：

```bash
docker compose --env-file /etc/tikhub-gateway/stage1.env ps
docker compose --env-file /etc/tikhub-gateway/stage1.env logs --since=5m --tail=100 apisix clickhouse
```

关注 `clickhouse-logger`、`failed to connect`、`server returned status code`、重试失败、待处理条目丢弃等错误。日志含环境信息，分享前只截取必要片段；不要分享密钥文件或完整管理配置。容器 JSON 日志有轮转上限。

## 隔离验收与阶段六收尾

以下仅在受控运维窗口执行。会建立两个临时测试身份和入口，全部真实连通性测试仍单独受一次额度限制：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v
docker compose --env-file /etc/tikhub-gateway/stage1.env --profile acceptance up -d --pull never --wait
python3 gatewayctl.py init-tests
python3 gatewayctl.py apply-mock
python3 acceptance_auth.py
python3 acceptance_proxy.py
python3 acceptance_quota.py
python3 acceptance_audit.py
# 维护窗口可选：python3 acceptance_outage.py（短暂停止本项目 ClickHouse）
python3 gatewayctl.py health --probe
python3 gatewayctl.py cleanup-tests
docker compose --env-file /etc/tikhub-gateway/stage1.env --profile acceptance stop mock
```

验收脚本不打印 key；鉴权验收会轮换测试 key，配额验收使用 3 次小额度后恢复默认。失败后先 `apply-users` 恢复清单期望状态，检查原因，再执行清理。测试入口只允许测试身份；正常运行不启动 `acceptance` profile。

重建仅限本项目，保留持久卷：

```bash
docker compose --env-file /etc/tikhub-gateway/stage1.env up -d --pull never --force-recreate --wait --wait-timeout 180
python3 gatewayctl.py health --probe
```

不要使用 `down -v`。私有环境文件（尤其数据加密 key）和员工清单须在受控位置加密备份，不随代码或验收包分享。etcd 损坏时可在空配置存储上，从同一仓库启动并用原私有环境和清单依次 `apply-users`、`apply-live`、`apply-audit` 重建本项目资源；恢复不会自动恢复已丢失的本地计数。

ClickHouse 数据备份在维护窗口先停本项目 `clickhouse` 服务，对命名卷 `tikhub-gateway_clickhouse_data` 做一致性冷备份，然后启动并执行 `health --probe`。恢复时在相同版本空实例上还原卷，并保留原环境凭据。环境/员工清单与数据卷一起做演练，不能只备份 Git。

修改 Compose 内嵌配置、XML、SQL 或其他文件型配置后需要显式重建对应服务（`up -d --force-recreate --wait <服务>`），Compose 不保证依据文件内容自动重启。初始化 SQL 只在首次建库执行，后续表结构变更需代码化迁移并单独验收。

变更从仓库提交、同步后部署；不在服务器手改 YAML、Lua 或 SQL。基础设施通过 Compose 编排，动态策略由仓库 CLI 调用 Admin API；员工清单是运行时数据。APISIX Dashboard 仅排查使用，正式策略变更回到 CLI/代码，避免配置漂移。

官方参考：[APISIX Dashboard](https://apisix.apache.org/docs/apisix/dashboard/)、[key-auth](https://apisix.apache.org/docs/apisix/plugins/key-auth/)、[proxy-rewrite](https://apisix.apache.org/docs/apisix/plugins/proxy-rewrite/)、[limit-count](https://apisix.apache.org/docs/apisix/plugins/limit-count/)、[ClickHouse logger](https://apisix.apache.org/docs/apisix/plugins/clickhouse-logger/)、[TikHub 账户信息](https://docs.tikhub.io/186826050e0)、[官方 API 市场](https://user.tikhub.io/dashboard/api-marketplace)、[ClickHouse 25.8 LTS](https://clickhouse.com/blog/clickhouse-release-25-08)。


## 内部管理网页（8081）

管理页位于 `http://118.196.147.13:8081`，固定用户名 **admin**，密码由环境变量 **ADMIN_PASSWORD** 注入。未登录访问列表、创建、启停或用量均跳转 `/login`。本期不设 IP 白名单，不做多人角色系统；原问卷服务 8080 保持原状。

页面支持姓名/飞书邮箱建档、子 key 前 8 位、创建时间、启停、今日用量及 CSS 柱状图。新 key 完整值只在创建后的结果页显示一次；刷新、返回或重新登录不再显示。请当场保存；确需找回时由可信 root 运维通过既有 `user-key` 交互命令处理。重启会使所有登录会话和待展示结果失效；它不会删除私有员工清单或轮换已发放的子 key。

### 初始化和启动

此功能需要 Docker Compose **≥2.30.0**（使用 `env_file.format: raw`，避免密码中的 `$`、`#`、引号被解释）。所有命令在本仓库 `deploy/tikhub/` 下由 root 执行；先确认 8081 和 `ADMIN_WEB_IP`（默认 `10.203.90.10`）空闲。

```bash
cd /opt/tikhub-gateway/deploy/tikhub
# 仅已有旧清单安装需要迁移。先检查，再执行；自动保存 root 私有备份。
python3 admin_migrate.py --dry-run
python3 admin_migrate.py
# 无回显设置 ADMIN_PASSWORD，两次输入；密码不出现在命令历史中。
python3 admin_setup.py
# 解析验证不输出密码；不执行完整 config 输出。
docker compose --env-file /etc/tikhub-gateway/stage1.env --profile admin config --quiet
docker compose --env-file /etc/tikhub-gateway/stage1.env --profile admin build admin-web
# APISIX 静态配置新增管理容器地址 /32，首次必须重建使其生效。
docker compose --env-file /etc/tikhub-gateway/stage1.env up -d --no-deps --force-recreate --wait apisix
docker compose --env-file /etc/tikhub-gateway/stage1.env --profile admin up -d --no-deps --wait admin-web
```

本机无法直连 Docker Hub 时，先从官方仓库准备 Dockerfile 所固定摘要的 amd64 Python 基础镜像，核验 blob 和传输文件 SHA256 后 `docker image load`。本次缓存为 `/var/cache/tikhub-gateway/admin-python-amd64.oci.tar`，SHA256 `9010aa1c564fdf701a55476060c41b8d6eab3ab043a7e1af499822a9c5da8c97`。本次在既有 Docker 引擎使用以下离线基础镜像构建方式；pip 从官方 PyPI 下载固定版本依赖，不修改 Docker 全局设置：

```bash
DOCKER_BUILDKIT=0 docker build --pull=false -f admin-web/Dockerfile -t tikhub-admin-web:2026-10-21 .
```

`admin_setup.py --initialize-only` 仅预备凭据和只读账号，不设置网页密码；没有 `ADMIN_PASSWORD` 时网页拒绝启动。私有 `/etc/tikhub-gateway/admin.env` 含 `ADMIN_PASSWORD=...`；`web-cli.env` 只含 APISIX Admin key、容器内可信 URL 及 ClickHouse reader 密码。两个文件均 root:0600，不提交 Git、不打进镜像。已有 gateway 配置和总 key 保留在原 `stage1.env`，该文件不挂到管理容器里。无需修改主机防火墙、问卷配置或 Docker 全局配置。

管理容器仅挂员工清单的专用目录和最小 CLI 凭据，根文件系统只读，去掉 capabilities，无 Docker socket。ClickHouse 的 `gateway_reader` 仅有 `SELECT ON tikhub.gateway_access`，通过持久 SQL 用户配置，不需重启 ClickHouse。APISIX 管理接口依旧仅在宿主机 127.0.0.1:9180 发布，容器桥接访问只新增专用管理容器 /32。

默认管理员密码在环境变量中；root/Docker 管理员可读取，这是本项目可信边界。当前 HTTP 不加密浏览器与服务器之间的密码及 session，沿用用户本期选择；后续切 HTTPS 时更新 `ADMIN_ORIGIN`、允许 Host 并启用 Secure Cookie。不要将上游 TikHub 总 key 作为管理页密码。

### 验证

```bash
docker compose --env-file /etc/tikhub-gateway/stage1.env --profile admin ps
curl -s -o /dev/null -D - http://127.0.0.1:8081/
# 预期 303、Location: /login；不应看到员工/key 信息。
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/login
# 预期 200。再用浏览器输入 admin 和自己设置的密码。
```

浏览器确认首页现有人数正确、密码错误无法进入、用量页可查；可创建一个专用测试身份、只复制一次完整 key，再停用/启用。无需访问 TikHub 来验证管理功能。未被本 CLI 私有清单管理的 APISIX Consumer 在页面只读，不能被启停。停用人员仍在列表，可启用，保留历史统计。

今日指北京时间当天零点到次日零点，使用 ClickHouse `received_at`、认证后的员工 ID、业务路径 `/tikhub/api/v1/`，按 `request_id` 去重。异步日志有数秒延迟，极端故障可能少量丢失。统计为调用次数，不是 TikHub 账单金额；数据源故障显示错误，不冒充 0 次。

本地隔离测试（不调用远端及 TikHub）：

```bash
python3 -m unittest test_gatewayctl test_web_ops test_admin_setup test_live_validation -v
python3 -m venv /tmp/tikhub-admin-tests
/tmp/tikhub-admin-tests/bin/pip install -r admin-web/requirements.txt httpx==0.28.1
cd admin-web
/tmp/tikhub-admin-tests/bin/python -m unittest test_app -v
```

### 密码更换、停用与回退

```bash
cd /opt/tikhub-gateway/deploy/tikhub
python3 admin_setup.py --password-only
# 重建后旧会话全部失效，新密码从私有 env_file 注入。
docker compose --env-file /etc/tikhub-gateway/stage1.env --profile admin up -d --no-deps --force-recreate --wait admin-web
# 只关闭网页入口，业务 API 保持运行：
docker compose --env-file /etc/tikhub-gateway/stage1.env --profile admin stop admin-web
```

回退到旧 CLI 前，先停网页，再执行 `python3 admin_migrate.py --rollback` 将最新清单回到旧路径（不是恢复陈旧备份），随后切回部署前代码。不要在新旧路径同时创建不同员工清单。私有备份仅用于故障恢复，不进入 Git。单独停止网页不会撤销已有员工 key；需要停用员工时用 CLI 或在网页停止前操作。
