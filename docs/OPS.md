# UserLoop 运维手册（N3 运维加固）

运维四件事：**看得见（健康/指标）· 报得出（告警）· 兜得住（备份恢复）· 压得动（压测）**。
全部零外部依赖，单机可跑；需要对接 Prometheus/告警平台时用下面两个出口。

## 1. 健康检查

```
GET /api/v1/health            # 需登录会话或 X-UserLoop-Token
.venv/bin/userloop ops health
```

返回 `status`（`ok` / `degraded`）与逐项检查：

| 检查项 | 含义 | 失败判定 |
|---|---|---|
| `store` | 主库可读（counts 可查询） | 查询异常 |
| `event_store` | 事件后端可用（SQLite / MySQL） | backend 为空 |
| `ingest_freshness` | 最近事件时间与年龄（阈值交给告警） | 查询异常 |
| `disk` | `data_dir` 可用比例 | 可用 < 5% |
| `scheduler` | 进程内 APScheduler 是否运行 | 未运行（CLI 检查时跳过） |
| `tenants` | 多租户目录计数 | 目录不可读 |

## 2. 指标（Prometheus 文本）

```
GET /api/v1/metrics            # 默认 text/plain; version=0.0.4
GET /api/v1/metrics?format=json
.venv/bin/userloop ops metrics [--json]
```

指标名（标签基数是安全的：HTTP 路径按**路由模板**归一）：

- `userloop_http_requests_total{method,path,status}`
- `userloop_http_request_duration_ms_*{path}`（桶 + sum + count）
- `userloop_events_ingested_total{source}`
- `userloop_loops_created_total` / `userloop_canvas_runs_total`
- `userloop_actions_total{type,channel,result}`（result=ok/blocked/deferred/failed）
- `userloop_scheduler_job_runs_total{job,result}`、`userloop_scheduler_job_duration_ms_*{job}`

抓取需要带 token 头（未开放匿名）：

```yaml
# prometheus.yml
scrape_configs:
  - job_name: userloop
    metrics_path: /userloop/api/v1/metrics
    authorization: {type: Bearer, credentials: unused}
    http_headers:
      X-UserLoop-Token: {values: ["<api_token>"]}
```

## 3. 告警

规则确定性、带冷却（默认 360 分钟同码只报一次），状态在 `data/ops/alert_state.json`，
历史在 `data/ops/alerts.jsonl`。配置：

```json
{ "ops": { "alerts": {
  "enabled": true,
  "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx",
  "thresholds": {
    "ingest_stalled_minutes": 180,
    "disk_free_pct_min": 10,
    "error_rate_pct_max": 5,
    "error_rate_min_requests": 20,
    "cooldown_minutes": 360
  } } } }
```

| 告警码 | 级别 | 触发 |
|---|---|---|
| `store_down` / `eventstore_down` / `scheduler_down` | high | 对应健康项失败 |
| `ingest_stalled` | high | 有历史数据但超过 N 分钟无新事件 |
| `disk_low` | high | 可用比例低于阈值 |
| `error_rate_high` | medium | HTTP 5xx 比例超阈值（样本数达标才判） |

```
GET  /api/v1/ops/alerts         # 当前告警 + 历史
POST /api/v1/ops/alerts/run     # 评估并按冷却窗口外送
.venv/bin/userloop ops alerts [--run]
```

调度：每 10 分钟自动评估一次（`alerts` job）。

## 4. 备份与恢复

- **一致性**：运行中的 SQLite 用 `VACUUM INTO` 生成一致副本（不会拷到半写状态）。
- **打包**：`data/backups/backup-YYYYmmdd-HHMMSS-mmm.tar.gz`，内含 `data/` + `BACKUP-MANIFEST.json`。
- **保留**：默认最近 7 份，超出自动清理（`ops.backup.keep`）。
- **调度**：每日 03:00 UTC 自动备份（`backup` job，覆盖整个 `data_dir`）。

```
.venv/bin/userloop ops backup [--keep 7] [--note "..."]
.venv/bin/userloop ops backups
.venv/bin/userloop ops verify <文件名>
.venv/bin/userloop ops restore <文件名> --force     # 恢复后需重启服务
```

API：`GET /api/v1/ops/backups`、`POST /api/v1/ops/backup`、`GET /api/v1/ops/backups/verify?name=`

恢复安全性：先校验（gzip/manifest/SQLite 头）→ 把现有目录改名留存 `data.bak.<ts>` → 再落新数据，
因此**恢复本身可回滚**。恢复会覆盖当前数据，故必须显式 `--force` / `force=true`。

> ⚠️ **事件表在外部 MySQL 时**：SQLite 快照**不含**事件表。manifest 会标注
> `events_backend=mysql`；请另行对 events 库做 `mysqldump` 并纳入同一备份窗口。

## 5. 压测

```bash
# 在服务器本机跑（绕过 Cloudflare 与公网）
.venv/bin/python deploy/loadtest.py --base http://127.0.0.1:8600/userloop \
    --token <api_token> --mode write --n 2000 --concurrency 20
.venv/bin/python deploy/loadtest.py --base http://127.0.0.1:8600/userloop \
    --token <api_token> --mode read --n 500 --concurrency 10
```

输出 RPS、错误数、p50/p95/p99。写入模式会生成 `loadtest_*` 用户与 `loadtest_event` 事件，
生产压测后可按前缀清理或走合规中心删除。


## 6. 异地备份（Cloudflare R2）

把本地备份归档推送到 R2（S3 兼容），实现**异地容灾**。纯标准库 SigV4（仅用 httpx），无需 boto3。

配置（`data/config.json`）：

```json
{ "ops": { "offsite": {
  "enabled": true,
  "endpoint": "https://<account_id>.r2.cloudflarestorage.com",
  "bucket": "nownexts-static",
  "region": "auto",
  "prefix": "userloop-backups",
  "keep": 7,
  "access_key": "...", "secret_key": "..."
} } }
```

行为：
- `userloop ops backup`（及每日调度、`POST /api/v1/ops/backup`）在本地备份成功后**自动上传 R2 并按 `keep` 清理远端**。
- R2 不可达时**静默降级**（本地备份照常，结果里带 `offsite.ok=false`）。
- 事件表在外部 MySQL 时，SQLite 快照不含 events，需另行 `mysqldump`（manifest 已标注）。

CLI：
```
userloop ops backup                 # 建本地备份 + 自动异地化（按配置）
userloop ops backup --no-offsite    # 仅本地
userloop ops offsite-list           # 列出 R2 上的备份
userloop ops offsite-prune --keep 7 # 清理远端旧档
userloop ops offsite-fetch <key>    # 从 R2 拉回（异地恢复演练/真恢复）
```

> 隔离建议：备份 key 前缀（如 `userloop-backups/`）与公开资产前缀（`assets/`）不同，
> CDN 只映射 `/assets/*`，因此备份对象**结构上不可公开访问**——但更硬的隔离是给备份用**独立桶**。
