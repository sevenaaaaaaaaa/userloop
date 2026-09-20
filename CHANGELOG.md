# CHANGELOG

## 0.6.1 — 备份修复（2026-09-18）

**修复**：① 备份排除 SQLite 边车文件（`-wal/-shm/-journal`）——它们属原库，入包会在恢复时
污染 VACUUM 快照；② `VACUUM INTO` 改用普通读写连接（只读连接遇到 WAL 会报
"attempt to write a readonly database"）；③ manifest 事件后端识别与健康检查同源
（`storage.events`），MySQL 事件库会明确标注需另行 mysqldump。新增活库 WAL 场景测试。

## 0.6.0 — 运维加固（N3，2026-09-18）

**健康**：`GET /api/v1/health` 深度检查（存储/事件后端/入库新鲜度/磁盘/调度器/多租户）+ `userloop ops health`。
**指标**：内置进程指标注册表 + 中间件，`GET /api/v1/metrics`（Prometheus 文本 / JSON）——
请求计数与时延直方图、入库/生成 Loop/动作/调度任务；路径按路由模板归一防标签爆炸。
**告警**：确定性规则（存储/事件后端/调度器/入库停滞/磁盘/5xx 率）+ 冷却去重 + 可选 webhook 外送，
状态与历史落盘（`data/ops/`）；每 10 分钟自动评估。
**备份**：`VACUUM INTO` 一致性快照 + tar.gz + manifest + 保留策略 + 校验 + 可回滚恢复；
每日 03:00 UTC 自动备份；CLI `userloop ops backup|backups|verify|restore`。
**控制台**：新增「运维健康」面板（检查项/当前告警/备份列表 + 立即备份/校验）。
**压测**：`deploy/loadtest.py`（ingest/overview 并发压测，RPS + p50/p95/p99）。
**文档**：`docs/OPS.md`。

## Unreleased — N3 Agent 团队 + N4 平台化（及 N1 收尾）

**N3**：角色化 Agent（分析/内容/触达/客服）+ 确定性配方拆解目标。`本月复购率 +10%` 落库为战役：低风险先跑，中风险等人审；批准后起草停用态 Loop，不直接对外触达。
**N4**：插件市场四类（source/action/model/template）；OpenAPI `/api/v1/openapi.json` + `/api/docs`；MCP 写工具只走审批状态机；租户用量台账，`billing.enforce` 才拦截。
**N1 收尾**：MFlow `mflow_item` 选题身份；主站 `track.js` 顺表单 identify；Hub 抬嵌套邮箱；契约探测 + 控制台面板。

## 0.5.3 — 控制台输出转义收口（2026-09-18）

**安全**：控制台（index/canvas）所有外部数据渲染经 `esc()`；内联事件参数经 `jsArg()`
（JSON 序列化 + 属性转义）或 `Number()`；新增 `tests/test_console_xss.py` 防回归。
**收尾**：修正预测面板 9px 横向溢出（`.wrap` 最小宽 140→96）；服务端周报 `_md_to_html` 确认已 escape。

## 0.5.2 — 控制台 UI/性能专项（2026-09-18）

**UI/对齐**：修复阶段列中文竖排断行、自诊断证据 JSON 撑破面板、长文本列改用折叠+悬停全文、
资产版本状态徽标语义色、资产 ID 省略号、移动端顶栏三列塌陷重叠。
**性能**：分区懒加载（首屏 API 从约 10 个降到 1 个；load 2784ms → 1238ms）。
**其他**：新增 esc() 转义、`.wrap/.nw/.ellip/.clamp*` 表格工具类、`.panels` 顶部对齐。

## 0.5.1 — 识别率攻坚（2026-09-18）

**变更**：跨系统身份映射（OpenFlow `member_id`/`visitor` 命名空间化绑定与跨设备归并）+
WebsFlow 页表单提交自动实名（`form_submit` + `identify`，不拦截提交）。
识别率是最大数据质量瓶颈，本轮把两条实名来源接通。

## 0.5.0 — 生态互联与运营资产市场（2026-09-18）

**变更**：N1 生态互联（inFlow 洞察→Loop 20 类配方、MFlow 发布回流→验证、WebsFlow 页事件回流+身份链接、
OpenFlow 画布双向调用、对话式触达框架）+ N2 运营资产市场（4 个行业包、导入导出、版本化回滚、审批门）。

## 0.4.0 — 生态互联与自进化（2026-09-18）

**动机**：UserLoop 已具备完整 MA 能力（旅程/AI 决策/触点/实验/预测/合规/多租户），
下一步是**生态互联**（线上 API 互通）与**自我进化**（像 OpenFlow 一样迭代）。

**新增**
- 生态与演进规划 `docs/ECOSYSTEM-AND-EVOLUTION.md`（四通道流转 + N0–N4 路线 + 自进化飞轮）
- 自进化机制 `userloop/evolve/`：运行遥测（`data/telemetry/*.jsonl`）、自诊断（8 类确定性规则）、
  AI 改进提案（配置白名单内可一键应用、代码类登记待办）、Lessons 库（自动生成 `docs/LESSONS.md`）
- 控制台「自进化」面板（自诊断问题 / 待批提案 / Lessons）+ CLI `userloop evolve status|propose|apply|lessons`
- 调度：每日遥测+自检；每周一自动生成改进提案
- 版本化：`VERSION` + 本文件 + `deploy/release.sh` 一步发版

**修复（Lesson）**
- 多租户串库：新租户默认继承主租户 MySQL 事件配置 → 事件会落进主库。已修并加回归测试
- 调度路径 `ctx.store` 未挂载 → 频控台账/投递回执静默失效。已修
- `data_span_days` 误把 `aiosqlite.Row` 当 dict → 自适应窗口塌成 1 天
- 周报 AI 叙事 400：强制 json_object 但输出 Markdown。已修（json_mode 可配）

## 0.3.0 — 识别 / 预测 / P1（2026-09-18）
识别（匿名→实名合并 + 归并连续性）、预测（churn/LTV/propensity 规则 v1 + 模型 v2 管线）、
STO、合规中心（consent/DSAR）、自然语言分群、跨渠道全局频控、Next Best Channel、AI 周报 + 批量审批。

## 0.2.0 — 触点与生态适配（2026-09-17）
触点抽象（邮件/H5/IM/短信/微信）、OpenFlow 桥（邮件+抑制名单）、WebsFlow 托管页与六维千面、
MFlow 线上产稿、A/B 版式实验、OpenFlow/MFlow 线上 API 互通。

## 0.1.0 — 首版（2026-09-16）
事件总线 + CDP-lite + 旅程引擎 + Loop 引擎 + Canvas + 触点执行器 + 登录 + 服务器部署。
