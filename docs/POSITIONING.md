# UserLoop 定位：独立全域营销数据中枢

> vs MFlow（内容营销流水线）、vs OpenFlow（增长执行 OS）—— UserLoop 的差异化与独立性

## 一句话

**UserLoop 是独立的用户旅程 Loop 引擎与全域营销数据中枢**：自己收数（不依赖任何一家 CDP）、自己算旅程（自有 CDP-lite）、自己出触达（邮件/飞书/HubSpot/任意 MA）、自己验效果（目标验证 → 回流）。与家族系统是**可插拔关系，不是依赖关系**。

## 与 OpenFlow / MFlow 的边界

| | OpenFlow | MFlow | UserLoop |
|---|---|---|---|
| 本质 | 单站增长执行 OS（TIPS） | 内容营销流水线 | 用户旅程 × 运营 Loop |
| 数据 | 自有 CDP（站内埋点） | 文件+状态机（SEO/GSC 数据） | **全域数据中枢**：多源归一化 + 自有 CDP-lite |
| 触达 | 站内自动化（画布/模板） | 内容发布（需人工授权） | 全渠道触达：邮件/飞书/webhook/HubSpot/任意 MA |
| 独立性 | 独立产品 | 独立产品 | **不依赖 OpenFlow CDP** —— OpenFlow 只是众多数据源之一 |

## 差异化支柱

### 1. 独立全域营销数据中枢
多源入站归一化（`POST /api/v1/hub/ingest?source=` 或自动识别）：
- **电商**：Shopify 订单 webhook（paid/refund → purchase/refund 旅程事件）
- **广告平台**：GA4 MP 风格、Segment 风格、自定义 webhooks
- **社媒/内容**：表单提交、关注/互动事件（webhook 转发）
- **外部 MA**：HubSpot webhook（contact 生命周期变更）
- **国内分析**：神策/GrowingIO 风格 `{distinct_id, event, properties}`

每源可选 HMAC 鉴权（`config.hub.sources.<source>.secret` + `X-Hub-Signature`），原始报文落 `data/hub/<source>.jsonl` 可对账回放。

### 2. 对接更多 MA（不止自家触达）
- `ma.hubspot.contact_upsert`：旅程阶段 → HubSpot lifecycle_stage 同步（email 幂等 upsert）
- `ma.hubspot.note`：触达摘要写入联系人（自定义属性）
- `ma.webhook`：任意客户现有 MA（n8n/Zapier/自研）webhook + HMAC 签名
- 兼容数据入站：客户 MA 的 webhook 直接喂 `/api/v1/hub/ingest`

### 3. 不依赖原 CDP 赋能
UserLoop 自有 CDP-lite（users/events/stage_transitions + stats），无外部依赖即可全链路运转：
建档 → 旅程 → 断点 → Loop → 触达 → 验证回流。OpenFlow/MFlow/HubSpot/电商都是**可选的输入源或输出目标**，
拔掉任何一个，闭环不中断（所有外部通道失败自动降级 dry-run 落 outbox）。

## 与家族系统的协同（可选增强，非依赖）

```
真实站点(OpenFlow/任意) ──track.js──▶ ┌──────────┐ ──mail(DeepSeek 文案)──→ 用户
Shopify/广告/HubSpot ──hub/ingest──▶ │ UserLoop │ ──ma.hubspot──────→ 客户现有 MA
外部 MA webhook ─────────────────▶   └────┬─────┘ ──openflow.*──────→ OpenFlow CDP(可选)
                                          └──验证回流→ feedback → 模板效果
```

- OpenFlow：`openflow.webhook_insight` 把高价值旅程信号回推其 CDP，供 GrowthBrain 消费（可选）
- MFlow：`mflow.create_content` 把旅程断点变成内容选题（可选）
