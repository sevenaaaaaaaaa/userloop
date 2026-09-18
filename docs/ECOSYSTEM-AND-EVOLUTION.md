# UserLoop 生态与演进规划（v2，基于线上 API 互通）

> 前提事实（已上线验证）：你们所有系统都在同一台服务器上，**能力互通一律走线上 HTTP API**，
> 不存在本地服务调用或代码级耦合。任何一方宕机，其余系统按降级策略继续运转。

## 一、生态全景：系统 × 能力 × 流转

```
                    ┌────────────────────────────────────────────────┐
                    │                UserLoop（编排中枢）              │
                    │  旅程引擎 · AI 决策 · 触点交付 · 验证回流 · 多租户 │
                    └───┬──────────┬──────────┬──────────┬───────────┘
      ①事件流入(入)     │          │②动作派发  │③洞察回读  │④效果回流
   ┌────────────────────┘          │(出)      │(入)      └──────────────┐
   ▼                               ▼          ▼                         ▼
┌──────────────────┐   ┌──────────────────┐  ┌──────────────┐  ┌──────────────────┐
│ OpenFlow 全站 OS │   │ MFlow 内容工厂    │  │ inFlow 情报  │  │ WebsFlow 落地页  │
│ CDP/CRM/自动化    │   │ 12 阶段产稿状态机 │  │ 诊断/竞品    │  │ 单页/H5/千面     │
│ 邮件/短信/微信    │   │ 多平台分发        │  │ 洞察→动作    │  │ 数据回流         │
└──────────────────┘   └──────────────────┘  └──────────────┘  └──────────────────┘
```

### 已上线的流转（本轮之前的成果，均为线上 API）

| 通道 | 链路 | 端点上 |
|---|---|---|
| ① 事件流入 | OpenFlow `cdp_event_received` 钩子 → UserLoop `/api/v1/ingest` | 已实测（`source=openflow` 事件落库）|
| ① 事件流入 | 站点 `track.js`（OpenFlow 页面注入）→ `/api/v1/track` | 已实测 |
| ① 事件流入 | 电商/广告/社媒/外部 MA → `/api/v1/hub/ingest`（自动识别格式） | 已实测（Shopify 订单→paying）|
| ② 动作派发 | UserLoop → OpenFlow 桥（邮件/微信/企微/短信出口、抑制名单） | 已实测（HTML 邮件 250 Ok）|
| ② 动作派发 | UserLoop → MFlow `/api/loop/create`（旅程断点→内容选题） | 已实测（产稿 1813 字，S4-qa）|
| ② 动作派发 | UserLoop → WebsFlow `/api/projects`（建页→发布→SSR 托管页） | 已实测（六维千面）|
| ② 动作派发 | UserLoop → HubSpot / 任意 MA webhook | 适配器就绪 |
| ③ 洞察回读 | OpenFlow Agent → UserLoop MCP（dashboard/journey/loops/feedback） | MCP Server 上线 |
| ④ 效果回流 | 邮件打开/点击/退订 → UserLoop 事件总线；MFlow 发布 webhook → ingest | 已实测（`email_open/click/unsub`）|
| ④ 效果回流 | OpenFlow CDP 回推（`openflow.webhook_insight`，HMAC） | 已实测（`userloop_signal` 落 OpenFlow）|

### 本轮（N1 首批）已打通 ✅

| 链路 | 实现 | 实测 |
|---|---|---|
| **inFlow 洞察 → UserLoop Loop** | `userloop/integrations/inflow.py`：拉取洞察（线上 `https://nownexts.com/inflow/api/v1/insights?workspace_id=insflow-main`）→ 按类型套**配方**（20 类）→ 复用 `copilot.validate` 白名单校验 → 生成 **Loop 草稿**（默认 disabled）→ 回执 `ack` 让洞察进入 acknowledged | ✅ 真实拉取 20 条洞察，全部生成对应草稿（RFM 风险→挽回、留存下降→召回、旅程缺口→激活、舆情负面→危机响应、LTV:CAC→高价值运营、关键词机会→MFlow 内容…）；回执后 inFlow 状态流转（下一批继续流入）|
| **MFlow 发布 → UserLoop 验证窗口** | `POST /api/v1/hub/mflow/publish` 登记发布 + 开窗（默认 14 天）→ 到期对比窗口内外归因事件（utm_campaign / url，**OR 去重**）→ verdict 写 `feedback`（模板 `content.mflow`）| ✅ 发布登记 → 窗口内命中 8 事件 → `effective`；**MFlow 侧已配置** `run/cms.json` → `http://127.0.0.1:8600/userloop/api/v1/hub/mflow/publish?token=…`，用其真实 `webhook.py` 适配器调用返回 `{ok: True}` 并成功登记验证中 |
| **WebsFlow 页内事件回流** | 用其**官方扩展点** `data.global.tracking.custom` 注入回传脚本：载入 UserLoop tracker + 回传 `localStorage.wf_vid` 作为 `visitor_id` | ✅ 既有发布页已注入并实测：真实浏览器访问 → UserLoop 收到 `page_view`（page=/webflow/p/…）+ `websflow_link`（visitor_id）→ **身份图谱绑定 `websflow_visitor`**（跨系统身份链接）；UserLoop 此后生成的新页面**默认自带回传** |
| 定时任务 | 洞察每 30 分钟同步、内容每 6 小时检查到期、均已按租户遍历 | ✅ |

### 尚未打通（N1 剩余）

| 缺口 | 价值 | 依赖 |
|---|---|---|
| **UserLoop → OpenFlow 画布**：Loop 作为 OpenFlow 自动化节点 | 双引擎互调 | OpenFlow 连接器（已支持，配 base_url 即可） |
| **跨系统统一身份（余下）** | 全域归因 | ✅ WebsFlow visitor 已打通；余 OpenFlow `member_id` / MFlow 选题回传 |

## 二、UserLoop 在产品矩阵中的角色与护城河

| 系统 | 它是什么 | 与 UserLoop 的关系 |
|---|---|---|
| OpenFlow | 全站执行 OS（TIPS） | **执行末端 + 事件源头**：UserLoop 决策、OpenFlow 落地 |
| MFlow | 内容工厂（GEO/多平台） | **内容供给**：UserLoop 提选题，MFlow 产稿，人工授权发布 |
| WebsFlow | 投放落地页工场 | **落地页供给**：UserLoop 给意图与受众，WebsFlow 出页 |
| inFlow | 增长情报 OS | **洞察源头**：情报 → UserLoop 动作（需打通） |
| **UserLoop** | **用户旅程 × 运营大脑** | **唯一同时持有"旅程 + 决策 + 触点 + 验证"闭环的角色**——这是护城河 |

**不可替代性**：
1. **闭环最完整**：从事件到验证回流，其余系统各持一段
2. **决策层唯一**：AI 大脑（NBA）+ 频控/合规/审批门 + A/B + 预测，跨系统统一执行
3. **数据自主**：自有 CDP-lite + 分租户独立库（数据 100% 本地可控，符合"细胞式"原则）
4. **中立**：不绑定任何执行端（OpenFlow/MFlow/WebsFlow/HubSpot 都可替换）

## 三、产品演进路线（N0 → N4）

| 阶段 | 目标 | 关键交付 | 验收标准 |
|---|---|---|---|
| **N0（已达成）** | 单站点全旅程闭环 | 事件总线/旅程/Loop/触点/AI 决策/实验/预测/合规/多租户/双语 | 真实站点访客进来即形成 Loop 并验证回流 |
| **N1 生态互联** | 四通道全通，情报→动作最短闭环 | inFlow 洞察接入、MFlow 发布回流、WebsFlow 事件回流、跨系统身份映射 | 一条"竞品异动"情报能自动生成并执行运营 Loop，效果回流到情报系统 |
| **N2 运营资产市场** | 跨租户可复用的模板/分群/实验资产（数据不共享） | 行业模板包、模板版本化与审批、资产导入导出（JSON） | 新租户 5 分钟导入一套行业运营包并跑通 |
| **N3 智能体运营团队** | 多智能体协作（内容/触达/客服/分析各司其职，审批门下自主） | 角色化 Agent（各自目标+工具白名单）、跨 Agent 任务编排、预算与审批 | 一个运营目标（如"本月复购率 +10%"）由 Agent 团队自主拆解执行，人工只审批 |
| **N4 平台化** | 对外开放与商业化 | 插件市场（source/action/model/template）、OpenAPI + MCP 双向、租户计费与配额 | 第三方按四类插件接入；按租户用量计费 |

**北极星指标（贯穿各阶段）**：
识别率 → Loop effective 率 → 每用户触达带来的增量转化 → AI 决策采纳率 → 少打扰次数（体验）→ 租户数

## 四、自我进化机制（对齐 OpenFlow/MFlow 的四件套，并用自身能力进化自身）

OpenFlow/MFlow 的迭代飞轮 = **版本化交付 + 运行数据回流 + Lessons 机制 + Demo 驱动验收**。
UserLoop 在此基础上加一条独有能力：**用自身的 AI 大脑与审批门来进化自身**（dogfooding）。

| 机制 | 落地 |
|---|---|
| ① 版本化交付 | `VERSION`（一行版本号）+ `CHANGELOG.md`（每次改动含动机与效果）；`deploy/release.sh` 一步发版 |
| ② 运行数据回流 | `data/telemetry/*.jsonl`：事件吞吐、动作成功/失败/被拦、AI 调用与 token、模型/接口延迟、租户活跃 |
| ③ Lessons 机制 | `evolution_lessons` 表 + 自动生成 `docs/LESSONS.md`：每个真实缺陷/决策沉淀为"错误只犯一次" |
| ④ Demo 驱动验收 | 每个新能力必须有端到端 demo（CLI/控制台一键）；未配置新用户 5 分钟见到效果 |
| ⑤ **自诊断与自提案**（UserLoop 特色） | 每日自检（存储/队列/失败动作/孤儿 Loop/模型/识别率/拦截率/租户健康）→ 生成问题清单；每周由 LLM 基于遥测与问题生成**改进提案**（含影响、风险、验证方式）→ 走**审批门** → 配置类变更可一键应用并审计，代码类进入待办 |

**进化闭环图**：
```
运行 → 遥测采集 → 自诊断(规则) → 提案(AI) → 人工审批 → 应用(配置类) / 待办(代码类)
         ↑                                                      │
         └────────────── 效果验证（回流对比）←────────────────────┘
```

## 五、风险与边界

| 风险 | 应对 |
|---|---|
| 跨系统数据一致性与身份错配 | 身份图谱 + 合并审计；跨系统映射表（N1 交付） |
| API 变更导致断链 | 契约探测（capabilities 端点）+ 降级路径 + 每日探测告警 |
| 多租户串数据 | 独立目录/独立事件库（已修串库缺陷）+ 回归测试 |
| 自进化"乱改配置" | 配置变更白名单 + 影响/风险标注 + 审批门 + 一键回滚（记录旧值） |
| 合规（出海/国内） | consent 门 + DSAR + 语言/时区 + 数据本地化（每租户独立库） |
