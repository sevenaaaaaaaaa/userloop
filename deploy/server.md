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

## 本地日常

```bash
userloop serve                                    # 本地 http://localhost:8600
curl -X POST https://nownexts.com/userloop/api/v1/ingest \
  -H "X-UserLoop-Token: <token>" -H "Content-Type: application/json" \
  -d '{"distinct_id":"u1","event":"signup"}'
```
