# UserLoop 服务器部署接线（2026-09-16）

## 目标服务器

与 MFlow 同机同约定：`172.96.253.73`，SSH 端口 `28766`，用户 `root`
（私钥/known_hosts 约定见 `~/MFlow Dev/mflow/deploy/server.md`）

## 与 XMP/OpenFlow、MFlow 的隔离（用户硬性要求沿袭）

- **独立目录**：`/www/wwwroot/userloop/`
- **独立进程**：systemd 单元 `userloop.service`，仅绑 `127.0.0.1:8600`（MFlow 占 8088，inFlow 占 8400，80/443 为 XMP/OpenFlow）
- **独立数据**：`/www/wwwroot/userloop/data/`（SQLite WAL + outbox.jsonl + auth.json + config.json）
- **独立 URL**：`https://nownexts.com/userloop/` —— 仅在 nownexts.com.conf 的 :80 与 :443 vhost 内各追加一段 `/userloop/` ProxyPass，不动 OpenFlow 主站其他路径

## 部署形态

- 运行时：uv + Python 3.12 → `/www/wwwroot/userloop/.venv`（与 MFlow 同款 uv 管理方式）
- 前缀：`USERLOOP_PREFIX=/userloop`（页面/API/埋点上报全部前缀感知）
- 数据：`USERLOOP_DATA=/www/wwwroot/userloop/data`
- 登录：`data/auth.json`（bcrypt 多用户，直接复用 MFlow `/www/wwwroot/mflow/run/auth.json` 同名同密码账号；两把钥匙同库不同系统，改密互不影响）
- API 接入鉴权：`config.json` 的 `api_token`（ingest 端点需 `X-UserLoop-Token` 头；`/track.js` 上报端点保持公开以支持任意站点嵌入）

## 部署步骤（install-server.sh 自动执行 1-5）

1. 打包上传本仓库（排除 .venv/data/tests/.git）→ `/www/wwwroot/userloop/`
2. `uv venv .venv --python 3.12 && uv pip install -e /www/wwwroot/userloop`
3. 初始化 `data/config.json`（api_token 随机生成）+ 迁移 `auth.json`（从 mflow run 目录复制）
4. 安装 `deploy/userloop.service` → `systemctl enable --now userloop`
5. 追加 Apache 反代（幂等：已有 UserLoop 段则跳过）→ `apachectl configtest && systemctl reload httpd`
6. 验证：`curl -s https://nownexts.com/userloop/` 应返回登录页；`/api/login` 错密码返回 403

## 存储分层（2026-09-17，对齐 OpenFlow EventStore 演进）

- **events 主用 MySQL**：独立实例 `userloop-mysql.service` → `127.0.0.1:3307`
  - 数据目录 `/www/server/userloop-mysql/data`，配置 `/www/server/userloop-mysql/my.cnf`（低内存：buffer pool 64M、performance_schema OFF）
  - 凭据：`/www/server/userloop-mysql/credentials.txt`（600，root + userloop 业务账号）
  - 业务账号 `userloop@127.0.0.1` 仅授 userloop 库的 SELECT/INSERT/UPDATE/DELETE/CREATE/INDEX/ALTER
  - **与主 MySQL(:3306) 完全隔离**，不动现有库与宝塔配置
- **SQLite 兜底**：`/www/wwwroot/userloop/data/userloop.db`（users/loops/actions/canvas/feedback 等仍在 SQLite；events 作为兜底与备份保留）
- **降级策略**：MySQL 连不上 → 自动回 SQLite 并记 warning，闭环不中断（已演练：停实例 → 服务仍 200 → 恢复后自动切回）
- 运维命令：
  ```bash
  userloop db status          # 查看当前 events 后端与规模
  userloop db migrate-events  # SQLite → MySQL 幂等回填（可重复执行）
  systemctl status userloop-mysql   # 独立实例状态
  ```
- 迁移记录：SQLite 1064 条 → MySQL 1121 条（含切换后新写入）

## 触点通道（2026-09-17）

- **邮件**：经 OpenFlow 桥 `userloop-bridge`（多通道 + 抑制名单 + 追踪/退订）；本地 SMTP 兜底
- **H5/落地页**：经 **WebsFlow 后台** `https://nownexts.com/webflow/api`（服务账号 `userloop-bot@nownexts.com`，凭据 `data/websflow-bot.txt` 600）；公网页 `/webflow/p/<token>`
- **短信**：`touch.sms.provider` ∈ aliyun|tencent|webhook，现值 `enabled=false`（填 AK/SK + 报备签名模板即生效）
- **MFlow（长内容）**：线上 API `https://nownexts.com/mflow`（服务账号 `userloop-bot`/operator，凭据 `data/mflow-bot.txt` 600）；`create_content` 建 item + 入队，MFlow agent 自产稿并推进状态机
- **识别/合规/预测**：`POST /userloop/api/v1/identify`、`/api/v1/compliance/{consent,export,erase}`、
  `/api/v1/predictions/*`、`/api/v1/segments/nl`；H5 留资表单（`touch.lead_capture.enabled`）
- **回执**：邮件 `email_open/email_click/email_unsubscribed`、H5 `h5_view/h5_click` → 事件总线 → 旅程/验证回流

## 本地日常

```bash
userloop serve                                    # 本地 http://localhost:8600
curl -X POST https://nownexts.com/userloop/api/v1/ingest \
  -H "X-UserLoop-Token: <token>" -H "Content-Type: application/json" \
  -d '{"distinct_id":"u1","event":"signup"}'
```
