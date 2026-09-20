# UserLoop 定位：独立内核 × 家族深度联动

> vs MFlow（内容营销流水线）、vs OpenFlow（增长执行 OS）—— 本文按**已实现功能**自评，随功能演进修订。

## 一句话

**UserLoop 是独立的用户旅程 Loop 引擎与全域营销数据中枢，同时与家族系统（OpenFlow/MFlow）保持一等的 API 联动**：能独立运转，联动时 1+1>2，联动失败自动降级、闭环不断。

## 自评：定位 × 实际功能（2026-09-16）

| 主张 | 实现状态 | 备注 |
|---|---|---|
| 独立全域数据中枢（多源入站） | ✅ 已实现 | `/api/v1/hub/ingest` 自动识别 segment/ga4/shopify/hubspot/generic + HMAC；原始报文落盘可回放 |
| 广告/社媒直连适配器 | ⚠️ 部分 | 格式层已收（GA4 MP/Segment/webhook），平台级拉取适配器（Meta/Google Ads API）未做，属路线图 |
| 对接更多 MA | ✅ 已实现 | `ma.hubspot.*`（lifecycle 同步）、`ma.webhook`（任意 MA + HMAC）；n8n/Zapier 走通用 webhook |
| 不依赖原 CDP | ✅ 已实现 | 自有 CDP-lite 全链路自洽；**但联动 OpenFlow 是增值不是障碍**（见下） |
| 旅程自动成 Loop | ✅ 已实现 | 8 阶段旅程 + 断点 → 模板/Canvas Loop → 验证回流 |
| AI 个性化触达 | ✅ 已实现 | `ai.email/compose`（DeepSeek 生成 → SMTP 真发），失败降级模板 |

修正记录：初版定位写过"不依赖原 CDP 赋能"，容易读成"与 OpenFlow 切割"。**修正为：独立是内核属性，联动是可选增益** —— 两者的关系是插件式组合，不是替代。

## 与 OpenFlow / MFlow 的边界与联动

| | OpenFlow | MFlow | UserLoop |
|---|---|---|---|
| 本质 | 单站增长执行 OS（TIPS） | 内容营销流水线 | 用户旅程 × 运营 Loop × 全域数据 |
| 数据 | 站内 CDP | SEO/内容数据 | **全域多源** + 自有 CDP-lite |
| 角色 | 站点执行与转化 | 内容生产与 GEO | 跨系统的旅程编排与触达 |

### 联动协议（四通道，均为一等公民）

| 通道 | 方向 | 实现 | 状态 |
|---|---|---|---|
| ① 事件流入 | OpenFlow → UserLoop | OpenFlow 插件 `userloop-tracker` 钩子 `cdp_event_received` 旁路转发 → `/api/v1/ingest`（HMAC token） | ✅ |
| ② 触达/动作派发 | UserLoop → 外部 | `email`/`feishu`/`webhook`（直发）、`ma.hubspot.*`、`mflow.create_content`、`openflow.automation` | ✅ |
| ③ 洞察回读 | OpenFlow/Agent → UserLoop | REST API（登录门禁）+ **MCP Server**（`userloop mcp`：dashboard/journey/loops/feedback 四工具，只读） | ✅ |
| ④ 信号回推 | UserLoop → OpenFlow | `openflow.webhook_insight` → InboundReceiver HMAC → OpenFlow CDP（`userloop_signal` 事件，GrowthBrain 可消费） | ✅ 已实测 |

MFlow 联动纪律沿袭 inFlow docs/04：对 MFlow 走插件 + HTTP，不附加 MCP；发布永远停在人工授权后。

### ①+② 组合效果（已上线实测）

```
OpenFlow 站内行为(cdp_event_received) ─┐
OpenFlow 页面行为(track.js) ───────────┤
Shopify/广告/HubSpot(hub/ingest) ──────┤
外部 MA webhook ──────────────────────┘
              ▼
        UserLoop 旅程引擎（自有 CDP）
              ▼
   断点 → Loop → AI 邮件/飞书/HubSpot 同步
              ▼
   高价值信号回推 OpenFlow CDP（GrowthBrain 消费）
   旅程断点推送 MFlow 产稿（内容运营闭环）
```

## 不做什么

- 不做站内 CMS/课程/商店（OpenFlow 已有）——UserLoop 不吞并，只联动
- 不替任何系统发布（MFlow 铁律）/不写外部 MA 的敏感操作
- MCP / OpenAPI 可写，但写操作一律回到 UserLoop 自己的状态机 + 审批门（不直接改模板、不直接发触达）

## 下一步（按价值排序）

1. 广告平台拉取型 source 插件（GA4/Google Ads OAuth 拉取）→ `hub/ingest` 定时喂数
2. Canvas/模板效果数据反哺 OpenFlow 后台卡片（经其插件 API）
3. 战役效果自动对照验收口径（复购率窗口到期后回写 feedback）
