# TikHub 内部 Key 管理页设计

2026-10-21。用户要求：在现有 `/opt/tikhub-gateway` 项目增加 FastAPI + 简单 HTML 管理页，纳入同一 Compose，代码继续提交现有 GitHub 仓库 `jialingfan110-eng/apisix`，部署到 `root@118.196.147.13:/opt/tikhub-gateway`；采用用户名 admin、ADMIN_PASSWORD 环境变量密码及简单 session 登录，不配置 IP 白名单。

## 用户使用方式

首页显示员工标识（暂用姓名或飞书邮箱）、子 key 前 8 位、创建时间、状态、今日调用次数。可以输入姓名/邮箱创建一个独立 key、复制本次生成的完整值，以及停用/启用已有人员。单独的今日用量页按员工显示调用次数和 HTML/CSS 柱状图。时间按 `Asia/Shanghai` 的当天零点至次日零点计算。

页面仅管理员工子 key，不显示、回传或存储公司 TikHub 总 key。允许访问管理页的人具有全部子 key 管理权限；本期采用单管理员登录，不做角色权限、自助申请、总 key 轮换或费用核算。

## 已核实的现状

- 仓库为 Apache APISIX Fork，本地/服务器已同步 GitHub；用户已确认本次仍使用现有 GitHub，不需要新增 GitLab remote。
- 业务 APISIX 9080 对外；Admin 9180、ClickHouse 8123 只发布在宿主机 loopback。
- 两个现有 Consumer 归仓库 CLI 管理，Admin API 可读取其创建时间和当前 key。页面只提取前 8 位，其余不进入 HTML、浏览器脚本或网页日志。
- `gatewayctl.py user-key ID` 仅领取已有 key，并要求 TTY；不能当作新建命令。新建复用 `person-add` 的底层逻辑，增加专门的创建并返回一次结果的 CLI 接口，不更改现有 `user-key` 的含义。
- `user-disable` 删除 APISIX Consumer，但保留私有清单；首页将 APISIX 现存列表与清单中的停用人员合并，避免停用后无法再启用。
- 实际审计表是 `tikhub.gateway_access`，包含 employee_id、path、status、request_id、received_at；不新增金额列，不计算或宣称真实成本。
- 用户最新确认管理页使用宿主机 8081 → 容器 8080；不再迁移 8080 问卷服务，不修改其防火墙规则。

## 实现约定（本次实现选择）

1. 新增 `deploy/tikhub/admin-web/`，使用 FastAPI、Jinja2 和少量本地 CSS；容器进程监听 `0.0.0.0:8080`，单进程，固定镜像及 Python 依赖版本。无前端框架、外部 CDN 或额外数据库。
2. Web 的读取和变更均通过参数列表调用仓库 `gatewayctl.py`，不用 shell 拼接。CLI 扩展脱敏的列表、创建一次性结果、今日汇总命令；统计查询只读 ClickHouse，新增/启停只通过现有 Consumer 管理逻辑写 Admin API 和私有运行时清单。网页不直接写 ClickHouse/etcd。
3. 员工输入去除首尾空白、拒绝控制字符并限制长度；显示标识保持姓名/邮箱，内部沿用 `emp_...` ID。重复员工标识拒绝，不悄悄覆盖或轮换已有 key。历史/外部管理 Consumer 可显示，但没有私有清单的条目只读。
4. 新建返回的完整 key 只送入一次性结果页，刷新和返回不会重新显示；响应 `Cache-Control: no-store`，key 不进入 URL、访问日志、localStorage 或错误信息。单次结果使用有时限的进程内存交付，重启丢失时由运维走既有 CLI 处理，不自动再建账号。重复提交使用创建请求 ID 防止生成重复账号。
5. 新建记录固定创建时间，停用/启用不改变该时间。迁移已有人员时使用 Admin API 现有 create_time；没有可靠时间则显示“未知”，不编造日期。状态区分启用、停用、待同步；API 不可用时显示错误，不能伪装成空列表或用量为零。
6. 今日统计限业务路径 `/tikhub/api/v1/`，按已认证员工统计，用 request_id 去重减少异步重试重复日志的影响；已停用人员保留历史用量。数据展示注明异步写入有数秒延迟、故障可能少量丢失。

## 秘密、容器与网络

访问入口为 http://118.196.147.13:8081，沿用本期 IP + HTTP、不新增域名或 HTTPS。用户名固定 admin，密码只从 ADMIN_PASSWORD 读取，未设置或为空时拒绝启动。未认证的页面和操作一律 303 跳转 /login（静态样式与 /login 本身除外）。会话使用随机不透明 token，服务端内存存储，Cookie HttpOnly、SameSite=Strict、8 小时有效；重启失效，POST /logout 注销。密码恒定时间比较，登录有轻量尝试频率限制，登录和管理 POST 均有 CSRF/Origin/Host 检查。以后支持 HTTPS 时启用 Secure Cookie，本期 HTTP 配置 secure=false。

- 管理容器不挂载 TikHub 总 key 或 ClickHouse 管理密码，不挂 Docker socket；只获得 APISIX 管理凭据、ClickHouse 表级 SELECT 凭据和网页防跨站请求的随机密钥。
- 私有员工清单需能原子替换，迁到专用 `/etc/tikhub-gateway/consumer-store/consumers.json`，由仓库迁移命令加锁执行，文件维持 root:0600、目录 0700。CLI 默认路径及 README 同步更新。旧/新同时存在时拒绝覆盖；提供可验证回退，保留全部已有 key。
- 管理容器只挂该员工目录和最小 CLI 凭据；代码只读、根文件系统只读、去掉 capabilities、不提权、不使用宿主机网络。
- 在现有 Compose bridge 为管理容器保留一个检查过无冲突的地址；APISIX Admin allow_admin 只新增该地址 /32，保持 9180 不对公网发布。ClickHouse 新增仅 SELECT 审计表的网页账号。
- 用户明确取消 IP 白名单；不新增或修改主机防火墙规则，8081 所有管理功能必须通过登录校验。禁用代理头信任，启停和新建均用 POST，模板自动转义。
- 通过仓库无回显初始化工具将 ADMIN_PASSWORD 写入私有 .env，Compose env_file 注入容器；该文件不入 Git、日志或镜像。管理 session 密钥使用独立随机凭据，配置不得挂载 TikHub 总 key。

## 验收与交付

使用隔离的假后端验证列表、停用记录、北京时间边界、创建结果只显示一次、重复提交、防注入/跨站和无密钥泄露。服务器验收用专门测试身份建/停/启并清理，只检查 Admin API 和 ClickHouse，不发 TikHub 付费请求。必须检查匿名访问跳转、错误密码拒绝、成功登录、注销与过期、重启使旧会话失效、密码未配置拒绝启动，以及原业务容器未受影响。

交付代码、Compose、迁移及网络脚本、README、测试和脱敏验收结果到现有 GitHub 仓库。部署到 `root@118.196.147.13:/opt/tikhub-gateway`，部署前保存本项目可回退的代码版本和私有清单备份；其他项目的调整仅按另行确认的迁移方案执行。

## 部署前由用户完成

服务器 SSH 重新登录；通过准备好的无回显工具设置 ADMIN_PASSWORD，不在聊天中提供密码。GitHub 仓库和端口已确定，不再等待 IP 白名单或 GitLab 地址。

## 设计审阅

只读审阅仅提出管理入口及 HTTPS 的范围问题，已依据此前明确的一期 IP + HTTP / 不做域名 HTTPS 约定补入。未增加新的用户待决项；其余实现细节由实施者决定。2026-10-21 按用户最新授权改为 8081 + admin 单账号 session 登录，取消 IP 白名单；用户允许多智能体实现。
