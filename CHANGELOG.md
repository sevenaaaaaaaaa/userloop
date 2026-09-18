# CHANGELOG

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
