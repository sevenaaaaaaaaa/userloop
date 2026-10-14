# CLAUDE.md — UserLoop

## 项目简介

UserLoop 是面向独立产品人与小团队的全域用户运营工具：采集事件 → 用户旅程 → 自动形成运营 Loop → 触达执行（邮件/H5/短信/MFlow 长内容）→ 验证回流。提供 Web 控制台、REST API、`track.js` 埋点与 MCP 接入。详见 `README.md`、`docs/OPS.md`。

## 技术栈

- Python 3.12+，FastAPI + Uvicorn，APScheduler，Pydantic，Click/Rich CLI，MCP
- 存储：SQLite（aiosqlite）为主；生产 events 用独立 MySQL 实例（127.0.0.1:3307，aiomysql/pymysql），连不上自动回退 SQLite
- 打包：hatchling（`pyproject.toml`），CLI 入口 `userloop`
- 测试：pytest + pytest-asyncio；dev 依赖含 ruff、mypy

## 目录结构

```
userloop/        主包：server/（API+控制台）、core/、tracking/、touch/（触达）、agents/、ai/、segments/、
                 experiments/、predict/、integrations/、ops/、i18n/、web/、cli.py、config.py、mcp_server.py
tests/           pytest 测试
plugins/         web-tracker 埋点插件
deploy/          部署脚本（sync.sh、release.sh、install-server.sh、userloop.service、Apache 片段、verify-*.py）
scripts/         辅助脚本
docs/            文档
data/            运行时数据（gitignored）
```

## 常用命令

```bash
pip install -e ".[dev]"          # 安装（含测试依赖）
userloop init                    # 初始化工作区
userloop demo                    # 端到端演示
userloop serve                   # 控制台 http://localhost:8600
userloop ops health              # 深度健康检查
userloop mcp                     # MCP 接入
userloop db status               # 查看 events 后端
.venv/bin/python -m pytest tests/ -q   # 测试
ruff check .                     # lint（dev 依赖中提供，无项目级配置）
```

build：无单独构建步骤。

## 部署方式

- 目标：服务器 `root@172.96.253.73`，SSH 端口 `28766`，目录 `/www/wwwroot/userloop`，systemd `userloop.service`（`127.0.0.1:8600`），Apache 反代到 `https://nownexts.com/userloop/`（前置 Cloudflare）
- **双轨同步，唯一入口 `deploy/sync.sh`**（本地 pytest → gitleaks → `git add -A` 提交并 push `main` → tar+scp 上传 → 解压重启 → 远端 pytest → 线上验收 → CF 缓存刷新）：
  - GitHub 轨（公开仓库）：只推干净代码。`.gitignore` 挡住 `data/`、`.env*`、`*.db`、`*.log`、密钥文件等；`git add -A` 后 gitleaks 扫描暂存区，发现泄漏即中止
  - 服务器轨：从工作区打包（排除 `.venv`/`data`/`.git` 等，解压只增改不删），服务器运行时数据不受影响
  - 运维脚本不再硬编码凭据：`deploy/rebuild-config.sh` 读 `USERLOOP_AI_KEY`（缺省沿用旧 config 的 `ai.api_key`），`deploy/verify-touch.sh` 读 `USERLOOP_ADMIN_USER`/`USERLOOP_ADMIN_PASSWORD`（变量名见 `.env.example`）
  - 本地 `.git/hooks/pre-commit` 跑 gitleaks（不入库，新 clone 需自行安装）

```bash
bash deploy/sync.sh -m "feat: 说明"            # 完整双轨（默认提交信息 chore: sync <时间>）
bash deploy/sync.sh --dry-run                  # 测试 + gitleaks + rsync -n 预览；不提交/不推送/不上传/不重启
bash deploy/sync.sh --server-only              # 跳过 git，只发服务器
bash deploy/release.sh <version> "变更摘要"     # 更新 VERSION + CHANGELOG 后调用 sync.sh
bash deploy/install-server.sh                  # 首次安装服务器
```

- 分支：`main`；无 GitHub Actions，由手动执行 `deploy/sync.sh` 触发
- 服务器约定详见 `deploy/server.md`

## 注意事项

- 环境变量：`USERLOOP_PREFIX`（生产 `/userloop`）、`USERLOOP_DATA`（数据目录）
- 运行时配置在 `data/config.json`（含 `api_token`，ingest 需 `X-UserLoop-Token` 头）、`data/auth.json`（登录）等，均不入库、勿打印
- CF 刷新凭据来自 `~/OpenFlow Dev/data/cloudflare.json`，勿读取或复制
- `deploy/sync.sh` 会 `git add -A` 自动提交所有改动，运行前确认工作区
- 页面/API/埋点需保持前缀感知（`USERLOOP_PREFIX`），勿硬编码路径
