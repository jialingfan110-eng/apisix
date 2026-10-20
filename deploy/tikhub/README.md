# TikHub 统一网关：阶段一

本阶段只启动 APISIX + etcd，验证网关基础环境。**没有 TikHub 路由、Consumer、限流或 ClickHouse，也不需要 TikHub 总 key。** 根路径返回 404 是预期结果，不能据此认定 TikHub 业务已经接入。

目录只有两个文件：`docker-compose.yml`（含必要启动配置）和本 README。修改 Compose 后通过 Git 管理和部署，不在服务器上另建、手改 APISIX YAML。

## 部署内容

| 项目 | 设置 |
| --- | --- |
| APISIX | 官方 `apache/apisix:3.19.0-debian`，同时固定镜像摘要 |
| 配置存储 | 官方 `quay.io/coreos/etcd:v3.6.15`，同时固定镜像摘要 |
| 目标平台 | Ubuntu 22.04 / 24.04，Linux amd64（按本次实际服务器固定架构） |
| Compose | **v2.23.1 或更高的 v2 版本**，使用 `configs.content` 保存启动配置 |
| 9080 | 阶段一默认仅绑定服务器 `127.0.0.1`，尚未开放同事调用 |
| 9180 | 仅绑定服务器 `127.0.0.1`，有独立 Admin key 和 IP 白名单 |
| etcd 2379 / 2380 | 仅容器网络，未映射到宿主机 |
| 持久化 | 命名卷 `tikhub-gateway_etcd_data` 保存网关配置 |
| 日志 | Docker JSON 日志，每个容器最多 3 × 10 MB；访问日志不含 Query、请求头或请求体 |
| 网络 | 默认 `10.203.90.0/24`，网关 `10.203.90.1`；部署前检查与现有网络不冲突 |
| 内存上限 | APISIX 768 MB、etcd 512 MB，避免挤占同机其他业务 |

APISIX 镜像本身不包含 curl/wget，因此健康检查使用镜像已有的 Bash 发出本地请求，检查带认证的 Admin API 能否读取路由。etcd 单独执行 `endpoint health`。

## 1. 部署前检查

以下命令在服务器执行；当前用户需有 Docker 权限和 sudo 权限，root 用户可直接执行。

```bash
uname -m
docker --version
docker compose version
command -v openssl
command -v curl
df -h /var/lib/docker
free -h
ss -lntp '( sport = :9080 or sport = :9180 )'
docker network ls
ip -4 route
```

确认两个端口未被其他服务占用，Docker 服务正常，Compose 版本满足要求，磁盘有镜像与 etcd 数据的可用空间。建议基础环境至少 2 vCPU、2 GB 内存；这不包含后续 ClickHouse 的资源需求。

如果服务器已有同名 Compose 项目或数据卷，先确认所属业务，不能覆盖。若默认网段冲突，选择空闲网段，并在下文的外部环境文件中同时设置 `TIKHUB_NETWORK_SUBNET` 和 `TIKHUB_NETWORK_GATEWAY`；无需改服务器上的 YAML。

## 2. 放置仓库

在服务器上获取你的 Fork，或将本阶段的两个文件放入已有仓库的 `deploy/tikhub/`。以下以仓库根目录 `/opt/tikhub-gateway` 为例：

```bash
git clone https://github.com/jialingfan110-eng/apisix.git /opt/tikhub-gateway
cd /opt/tikhub-gateway/deploy/tikhub
```

若本次变更尚未推送到 Fork，需要先将这两个文件从本地工作副本同步到上述目录；只有上游代码的 checkout 不包含本阶段部署文件。

## 3. 生成服务器专用密钥

密钥文件放在 Git 仓库之外：`/etc/tikhub-gateway/stage1.env`，目录权限 0700、文件权限 0600。不要粘贴已有 TikHub 总 key，也不要把密钥文件提交到 Git。

下面的初始化重复执行会保留已有文件，不会偷偷轮换密钥：

```bash
sudo install -d -m 0700 /etc/tikhub-gateway
sudo sh -eu -c '
  if [ -e /etc/tikhub-gateway/stage1.env ]; then
    printf "%s\n" "已有密钥文件，保留原值。"
    exit 0
  fi
  umask 077
  admin_key="$(openssl rand -hex 32)"
  encryption_key="$(openssl rand -hex 16)"
  test "${#admin_key}" -eq 64
  test "${#encryption_key}" -eq 32
  printf "APISIX_ADMIN_KEY=%s\nAPISIX_DATA_ENCRYPTION_KEY=%s\n" \
    "$admin_key" "$encryption_key" > /etc/tikhub-gateway/stage1.env
'
```

Admin key 用于管理网关，数据加密 key 用于后续敏感配置字段，两者均不是 TikHub 总 key 或同事子 key。请将环境文件安全备份；后续存入 Consumer 后不要直接替换数据加密 key。

环境变量对服务器 root 和 Docker 管理员可见。本方案隔离的是同事的业务调用权限，并不对可信服务器管理员隐藏密钥。不要运行或分享会展开密钥的 `docker compose config` 完整输出、`docker inspect` 环境信息或 shell 调试跟踪。

## 4. 校验、拉取、启动

```bash
cd /opt/tikhub-gateway/deploy/tikhub
sudo docker compose --env-file /etc/tikhub-gateway/stage1.env config --quiet
sudo docker compose --env-file /etc/tikhub-gateway/stage1.env pull
sudo docker compose --env-file /etc/tikhub-gateway/stage1.env up -d --wait --wait-timeout 180
sudo docker compose --env-file /etc/tikhub-gateway/stage1.env ps
```

预期 etcd、apisix 都为 `healthy`。`config --quiet` 只检查结构，不输出密钥；它通过不代表容器或真实服务已启动。`up --wait` 超时后不能继续业务阶段，应先查故障。

### 本次服务器的离线镜像方式

本次服务器无法直连 Docker Hub，但可访问 Quay。etcd 从官方 Quay 拉取；APISIX 从官方 Docker Hub 在本机获取、核对原始索引与 amd64 镜像摘要后，经 SSH 传入服务器。没有配置第三方镜像源或修改 Docker 全局设置。

服务器保留的导入包为 `/var/cache/tikhub-gateway/apisix-3.19.0-amd64.oci.tar`，SHA256 为 `73a6246607da3668f54cd381dd4abf4e4e4ba781c295a7817cf33bd15a7de860`。这是公开镜像的运维缓存，不是密钥文件，也不提交到 Git。需要重新导入时：

```bash
printf '%s  %s\n' \
  73a6246607da3668f54cd381dd4abf4e4e4ba781c295a7817cf33bd15a7de860 \
  /var/cache/tikhub-gateway/apisix-3.19.0-amd64.oci.tar | sha256sum --check
sudo docker image load --input /var/cache/tikhub-gateway/apisix-3.19.0-amd64.oci.tar
sudo docker image inspect \
  apache/apisix:3.19.0-debian@sha256:5767cb637c584366bb932511d079ab87a38809ed7c5ce413e9a515ad85c8316f \
  --format '{{.Id}}'
sudo docker compose --env-file /etc/tikhub-gateway/stage1.env pull etcd
sudo docker compose --env-file /etc/tikhub-gateway/stage1.env up -d --pull never --wait --wait-timeout 180
```

`inspect` 必须能按 Compose 中的完整摘要找到本地镜像。新服务器如果没有这个缓存包，应从官方镜像仓库获取同一摘要的镜像或重新准备经过摘要验证的离线包，不能只下载同名标签后跳过摘要校验。

## 5. 验收

### 网关和管理边界

在服务器执行：

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9080/
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9180/apisix/admin/routes
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9180/ui/
sudo docker compose --env-file /etc/tikhub-gateway/stage1.env exec -T etcd \
  /usr/local/bin/etcdctl --endpoints=http://127.0.0.1:2379 endpoint health
```

预期依次为：**404**、**401**、**200（或跳转后 200）**、**is healthy**。Dashboard 静态页面可访问不代表管理 API 无需认证，缺 key 的管理请求必须被拒绝。

健康检查已使用正确 Admin key 读取路由。若要手动确认路由数，可执行下面命令；只打印条数，不打印密钥或配置内容：

```bash
sudo sh -eu -c '
  . /etc/tikhub-gateway/stage1.env
  printf "header = \"X-API-KEY: %s\"\n" "$APISIX_ADMIN_KEY" |
    curl --config - --fail --silent --show-error http://127.0.0.1:9180/apisix/admin/routes
' | python3 -c 'import json,sys; data=json.load(sys.stdin); print("route_count:", data["total"])'
```

全新部署的预期结果为 `route_count: 0`。本阶段不通过 Admin API 创建测试路由。

### 重建与持久化

下面只重建本项目容器，保留 etcd 数据卷及密钥：

```bash
sudo docker compose --env-file /etc/tikhub-gateway/stage1.env down
sudo docker volume inspect tikhub-gateway_etcd_data --format '{{.Name}}'
sudo docker compose --env-file /etc/tikhub-gateway/stage1.env up -d --wait --wait-timeout 180
sudo docker compose --env-file /etc/tikhub-gateway/stage1.env ps
```

预期数据卷仍存在，两个服务恢复 healthy。阶段一没有业务配置，这项验收仅验证卷保留和容器恢复；后续有 Consumer、路由后还需验证业务配置持久化。**不要加 `down -v`，那会删除配置数据。**

### 从自己的电脑查看

默认不把业务端口开放到公网。若要从电脑访问，可以建立 SSH 转发：

```bash
ssh -N -L 19080:127.0.0.1:9080 -L 19180:127.0.0.1:9180 root@服务器IP
```

访问 `http://127.0.0.1:19080/`，预期 404；Dashboard 地址为 `http://127.0.0.1:19180/ui/`。后续开放同事调用时才设置 `APISIX_LISTEN_IP`、限制公司出口 IP；9180 始终保持本机绑定，不开放公网。

## 6. 排查与停止

```bash
sudo docker compose --env-file /etc/tikhub-gateway/stage1.env ps
sudo docker compose --env-file /etc/tikhub-gateway/stage1.env logs --tail=100 apisix etcd
sudo docker compose --env-file /etc/tikhub-gateway/stage1.env stop
```

| 现象 | 处理 |
| --- | --- |
| `configs.content` 不支持 | 升级至 Compose v2.23.1 或更高的 v2 版本，再重新校验；不要临时拆文件绕过 |
| 缺少环境变量 | 检查 `--env-file` 参数和密钥文件是否存在，不要复制真实 key 到 Compose |
| 镜像拉取失败 | 检查服务器 DNS、到 Docker Hub / Quay 的连通性；不要换成未知第三方镜像 |
| 网段重叠 | 选择无冲突网段，在外部环境文件同时设置 subnet 和 gateway，再重建本项目网络 |
| 管理请求返回 403 | 记录访问来源与 Docker 网络信息，检查网关地址配置；不要放宽为 `0.0.0.0/0` |
| apisix unhealthy | 检查 etcd 健康、密钥配置、只读配置挂载及端口占用；不继续接入业务 |
| 9080 返回 404 | 阶段一尚无路由，这是正常结果 |

日志可能包含运行环境信息，排查时只提供必要片段，不提供环境文件、密码、完整请求或完整管理配置。

回退阶段一可执行上面的 `stop`，或使用不带 `-v` 的 `down`；保留仓库文件、密钥文件及 etcd 命名卷。

## 验收后的下一步

由你确认阶段一验收结果后，才开始阶段二的工号 Consumer 和个人子 key。TikHub 总 key、真实付费调用、限流与 ClickHouse 都在后续阶段逐项接入。

官方参考：[APISIX 3.19.0](https://github.com/apache/apisix/releases/tag/3.19.0)、[Dashboard](https://apisix.apache.org/docs/apisix/dashboard/)、[APISIX Docker 源码](https://github.com/apache/apisix-docker)、[etcd 3.6.15](https://github.com/etcd-io/etcd/releases/tag/v3.6.15)、[Compose configs](https://docs.docker.com/reference/compose-file/configs/)。
