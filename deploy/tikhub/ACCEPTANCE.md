# 2026-10-21 最终部署验收记录

部署目录 `/opt/tikhub-gateway/deploy/tikhub`，三个核心容器 APISIX / etcd / ClickHouse。本记录不含密钥、员工姓名、账户邮箱或余额。

| 项目 | 结果 |
| --- | --- |
| 总 key 安全录入 | 用户通过服务器 TTY 无回显录入；环境文件 root:0600，目录 0700 |
| 单元验证 | 13 项管理、安全边界及一次性验收请求保留检查通过 |
| 阶段二鉴权 | 12 项通过：两人独立身份、缺 key、错 key、Query-only、Bearer-only、重复头、伪造身份、停用及轮换 |
| 阶段三模拟透传 | GET/POST/PUT/PATCH/DELETE/OPTIONS，重复/编码 Query、二进制请求体、418 二进制响应、503 透传均通过 |
| 上游凭据隔离 | 模拟接口确认 Authorization 被覆盖，apikey 与伪造身份头不转发 |
| 阶段四限流 | 3 次成功后 429；不同员工独立；同一员工跨路由共享；已恢复 100 次/60 秒默认 |
| 阶段五审计 | 200/401/429/503 入库，身份归属正确，请求 ID 非空；未出现总 key、子 key、Query 或正文标记 |
| 最小权限 | ClickHouse 写入账号可接收日志但 SELECT 被拒绝 |
| 故障演练 | 停 ClickHouse 时模拟业务 200，约 3ms；观察到 logger 连接失败；恢复后异步探针成功 |
| 真实 TikHub | 修复后按 2026-10-21 单独授权调用 1 次，HTTP 200、业务码 200，账户信息返回正常，响应无密钥；对应审计恰好 1 条、员工归属及上下游 200 正确 |

官方 API 市场在验收前列出 `/api/v1/tikhub/user/get_user_info` 为 $0.0000/请求。本次未输出账户信息正文、邮箱或余额，未读取实际账单金额。首次尝试在 TLS 握手阶段返回 502；修复是在 NGINX 启动时设置 `proxy_ssl_verify on` 与 `proxy_ssl_verify_depth 5`，载入系统 CA 并保持证书校验。2026-10-21 用户明确授权额外一次，成功取得 HTTP 200 / 业务码 200；没有自动重试。原记录 `real-validation.json` 保留，追加授权记录独立保存为 `real-validation-authorized-20261021.json`，重复执行会在发请求前被拒绝。

9080 以 IP+HTTP 发布，9180/8123 保持本机绑定。本机外网实测缺 key 返回 401；重建后三项服务 healthy，员工及日志数据保留，41 个原业务容器的 ID/启动时间均未变。不可依据“容器 healthy”认定所有网络或真实业务已验收。临时身份和模拟入口在收尾时清理，正常启动不带 acceptance profile。

本方案不保证故障零丢日志，调用计数也不等同美元费用。正式切换仍需用户在 TikHub 后台撤销曾经直接发给同事的旧总 key，否则旧 key 可以绕过网关；本次不会自行更改 TikHub 账户凭据。


## 2026-10-21 内部管理页验收

入口 `http://118.196.147.13:8081`，admin + ADMIN_PASSWORD session 登录，不设 IP 白名单。APISIX 9080 继续处理业务，问卷服务 8080 保持原状。

- 73 项本地测试通过（52 CLI/迁移/原有边界测试 + 21 网页测试，后者包含 8 组并发注销/过期场景）。
- 远端真实 HTTP：匿名首页/用量/结果页跳转登录；错误密码拒绝；正确登录后首页与用量 200；临时身份创建、完整 key 仅展示一次、重复提交拒绝、停用/启用保持同 key、注销和容器重启使旧会话失效均通过。
- 临时验收身份已清理，原两位员工的 key 和状态不变；TikHub 总 key 环境文件逐字节保持不变。未发起 TikHub 上游请求。
- ClickHouse reader 实测 SELECT 成功、INSERT 因权限/readonly 被拒绝；不是用语法错误代替权限验收。`health --probe` 仅产生本地随机 404 并验证异步落库。
- 管理容器 healthy，根文件系统只读，无 Docker socket、TikHub 总 key、ClickHouse 管理/写入密码。Admin 9180 与 ClickHouse 8123 仍只发布到宿主机 loopback。
- APISIX、etcd、ClickHouse healthy，其他业务容器 ID 不变；构建产生并自动删除的临时容器不属于原业务。
- 源码和有界运行日志按实际秘密值扫描，未发现泄露；密码、私有备份和员工清单不进入 Git。

当前仍采用 HTTP。密码修改通过 `admin_setup.py --password-only` 后只重建 admin-web；后续 HTTPS 另行配置。
