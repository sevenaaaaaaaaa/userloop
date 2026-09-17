# 触点交付整合规划（全用户旅程 × 全渠道）

> 目标：UserLoop 作为旅程编排大脑，把"该在什么触点、交付什么内容"变成可执行能力。
> 原则：**复用优先**（OpenFlow / WebsFlow / MFlow 已有的不重造），**自建兜底**（关键体验与闭环数据必须自持），**合规与频控在服务端强制**。

## 一、能力基线盘点（2026-09-17 实测）

| 渠道 | 现成能力 | 位置 | 可直接调用 |
|---|---|---|---|
| **邮件** | 多通道（SMTP/BillionMail/Ghost/Webhook 自定义）、HTML、抑制名单、打开/点击追踪、归因、送达率中心 | OpenFlow `lib/MailChannel.php` · `EmailDeliverability.php` · `api/mail-track.php` | 需桥接（无公开发信 API） |
| **公众号** | 群发（按标签/按 openid）、预览、模板消息、客服消息、素材、用户标签 | OpenFlow `lib/WechatMp.php` · `api/wechat.php`（回调接收） | 需桥接 + 身份映射 |
| **企业微信** | 客户标签/分组、客户列表、应用消息、群发助手、客户群 | OpenFlow `lib/Wecom.php` · `api/wecom.php`（回调接收） | 需桥接 + 身份映射 |
| **IM 通知** | 企微/飞书/WhatsApp webhook | OpenFlow `lib/NotifyChannels.php`；UserLoop `feishu` 动作 | 已可用 |
| **短信** | 仅供应商配置（阿里云/腾讯云），**无发送实现** | OpenFlow `admin/sms.php` | ❌ 缺口（P1 自建或补桥接） |
| **落地页/H5** | 单页工场：模块化、千人千面、H5、数据回流、导出单文件 HTML | WebsFlow（Node API：projects/events/automation/referral…） | 需桥接 |
| **站内页/表单/分享** | CMS 页、表单、二维码（`qr.php`）、分享卡（`share-card.php`）、直播/课程页 | OpenFlow | 链接生成即可 |
| **内容生产** | 长文/多平台分发（Sanity/WordPress/webhook 适配器）、GEO 内容工厂 | MFlow | `mflow.create_content` 已接入 |
| **网页行为** | track.js（页面浏览/点击） | UserLoop | ✅ 已上线 |

**结论**：渠道实现几乎都在，缺的是 ①**统一触点抽象**（编排层）②**身份映射**（跨渠道找人）③**内容渲染**（模板/追踪/合规）④**回执回流**（送达/打开/点击/退订进入旅程）。

## 二、触点全景矩阵（旅程阶段 × 渠道 × 内容形态）

| 阶段 | 主触点 | 次触点 | 内容形态 |
|---|---|---|---|
| 访客 visitor | 落地页/H5（WebsFlow）、站内页 | 二维码/分享卡、广告落地 | 单页、模块化区块 |
| 注册 signup | 邮件（欢迎/验证） | 公众号关注引导、短信验证码 | HTML 邮件、H5 |
| 激活 activated | 邮件（引导）、站内引导位 | 企微 1v1、公众号模板消息 | 图文邮件、任务清单 H5 |
| 付费 paying | 邮件（收据/权益） | 企微客户私信、短信 | 交易型邮件、卡券 |
| 留存 retained | 邮件（内容/提醒） | 公众号群发、企微朋友圈 |  Newsletter、活动页 |
| 推荐 advocate | 邮件（邀请） | H5 邀请页（WebsFlow referral）、分享卡 | 邀请 H5 + 海报 |
| 流失风险 churn_risk | 邮件（召回） | 短信、企微私信、公众号模板 | 召回邮件、优惠券 H5 |
| 已流失 churned | 短信（低成本触达） | 邮件（最后一次） | 短文案 + 单页 |

## 三、统一触点模型（四段式 + 一道闸门）

```
Identity 身份图谱 → Content 渲染 → Delivery 投递 → Feedback 回执回流
                         ↑                    ↓
                    内容模板库        频控/静默期/退订/合规闸门（服务端强制）
```

### 1) Identity 身份图谱（前置条件）
跨渠道触达必须知道"这个邮箱/手机/openid 是不是同一个人"。
新增表 `identities(user_id, type, value, verified, source)`，type ∈
`email | phone | wechat_openid | wechat_unionid | wecom_userid | websflow_visitor`。
- 写入时机：埋点带 email/phone、微信回调（openid→unionid）、表单提交、WebsFlow 回流传参
- 解析服务：`resolve(user_id, type)` / `merge(user_a, user_b)`

### 2) Content 渲染
- 邮件：HTML 模板（布局 + 变量 + CTA + 追踪像素 + 退订链接），文本兜底
- H5/落地页：区块化模板（标题/正文/CTA/表单），千人千面变量
- IM/短信：短文案 + 链接（短链带追踪）
- AI 只产出**内容槽位**（标题/正文/CTA 文案），版式由模板保证，避免"AI 生成的丑邮件"

### 3) Delivery 投递（驱动注册表）
统一接口 `TouchDriver`，每个渠道声明能力并实现投递：

```python
class TouchDriver(Protocol):
    channel: str                     # email / sms / wechat_mp / wecom / h5 / web / im
    caps: set[str]                   # render/deliver/track/inbound/richtext
    async def deliver(spec, user, ctx) -> TouchResult   # {ok, ref, cost, degraded}
```

| 渠道 | 驱动实现 | 归口 |
|---|---|---|
| email | OpenFlow 桥（`mail_send` + 抑制名单）→ 失败降级自建 SMTP | 桥接 + 兜底 |
| im | feishu / wecom webhook | UserLoop 现有 |
| wechat_mp | OpenFlow 桥（模板消息/客服消息） | 桥接 |
| wecom | OpenFlow 桥（应用消息/客户私信） | 桥接 |
| sms | 阿里云/腾讯云直连（服务端自建，P1） | 自建 |
| h5 | WebsFlow API（建页 → 取链接） | 桥接 |
| web | OpenFlow 现成页/表单/二维码链接 | 链接生成 |

### 4) Feedback 回执回流
UserLoop 自持追踪端点（保证闭环数据自主）：
- `GET /t/e/open.gif?t=<signed>` → 记 `email_open` 事件（同时可选镜像 OpenFlow 归因）
- `GET /t/e/click?t=<signed>&u=<url>` → 记 `email_click` 事件 + 302
- 短信/微信回执（送达/失败/回复）经各渠道回调 → `/api/v1/hub/ingest`
- 退订/投诉 → 抑制名单（OpenFlow `email_suppress`）+ UserLoop 侧标记，永久不再触达

回执事件直接进入事件总线 → 驱动旅程阶段与 Loop 验证（例如把 `email_click` 设为目标事件，验证邮件是否真的有效）。

### 5) 合规与频控闸门（服务端强制，AI 与运营都无法绕过）
- 频控：单用户 24h/7d 上限、渠道级冷却、全局日预算（已有 AI 护栏，扩展到所有触点动作）
- 静默期：默认 22:00–08:00（可按渠道覆盖，短信更严）
- 退订/抑制：邮件必带退订链接；短信必带退订指令；微信遵守模板消息次数限制
- 审计：每次交付落 `actions` + `outbox`，内容与结果可回溯

## 四、邮件体验专项（当前最大短板）

**现状问题**：纯文本、本机 postfix 直发、无版式、无追踪、无退订、无退信处理、发件域未认证 → 打开率与送达率都不可控。

**整改方案（P0，全部复用 OpenFlow 已实现能力 + 自持追踪）**
1. 版式：统一 HTML 邮件模板（品牌头/正文/CTA/页脚 + 退订），AI 只填内容槽位
2. 投递：走 OpenFlow `mail_send()`（已是 `Content-Type: text/html`，多通道可切 BillionMail/Ghost/自定义），并前置 `email_is_suppressed()` 抑制检查
3. 追踪：主题带 UserLoop 签名令牌的打开像素与点击跳转（自持），并镜像 OpenFlow `mailc_track` 做跨系统归因
4. 退订：一键退订 → 进抑制名单（OpenFlow）+ UserLoop 标记 `email_unsubscribed` 事件
5. 送达率：发件域补 SPF/DKIM/DMARC（OpenFlow `EmailDeliverability` 自检可复用），退信/webhook 回执入库
6. 指标：送达率、打开率、点击率、退订率、投诉率进看板（目标：送达 ≥98%、投诉 <0.1%）

## 五、分阶段落地路线

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **P0（本轮）** | 触点抽象 `userloop/touch/` + 邮件驱动（OpenFlow 桥 + SMTP 兜底）+ HTML 模板 + 自持追踪/退订 + 身份表 + OpenFlow 桥接插件 | 一条 `touch.email` 动作：真实 HTML 邮件送达、打开/点击回流为旅程事件、退订进抑制名单 |
| **P1** | H5/落地页（WebsFlow 桥：建页→取链接→回传事件）、短信驱动（阿里云/腾讯云直连）、公众号模板消息/客服消息（OpenFlow 桥 + openid 映射） | 旅程断点可自动发 H5 邀请页与短信；公众号模板消息可送达 |
| **P2** | 千人千面版式（按阶段/来源切换 H5 区块与邮件模块）、A/B 版式实验与验证回流、企微 1v1 与客户群、WhatsApp/其他 IM | 版式级 A/B 有 effective 结论；企微私信纳入 Loop |
| **P3** | 内容资产中心（模板版本化/审批流/预览）、发送预热与配额治理、多语言（i18n）触点 | 模板变更可灰度；发件域预热曲线可控 |

## 六、与现有能力的关系（不重造）

- **OpenFlow**：邮件/微信/企微的**实现层**（我们只加桥接插件，不改其内核）
- **WebsFlow**：H5 与投放页的**生产层**（UserLoop 只负责"何时发什么链接"）
- **MFlow**：长内容与多平台分发的**生产层**（旅程断点 → 选题 → 成稿 → 人工授权后发布）
- **UserLoop**：**编排 + 身份 + 追踪 + 验证**（这是四者中唯一打通"旅程闭环"的角色）

## 七、风险与依赖

| 风险 | 影响 | 应对 |
|---|---|---|
| 微信需认证服务号/企微应用，openid 需用户授权获取 | P1 公众号/企微触达无法立即生效 | 先用邮件+短信+H5；等待公众号授权链路，身份表预留字段 |
| 短信供应商需实名与签名报备 | P1 短信延迟 | 先接一家（阿里云），签名用现有主体 |
| 自持追踪可能被邮件客户端拦截图片 | 打开率偏低 | 点击追踪为主指标，打开为辅；与 OpenFlow 归因互相校验 |
| 多渠道频控叠加导致"过度安静" | 触达不足 | 频控按渠道组配置（邮件/短信/IM 分组计数），AI 决策参考剩余配额 |
| H5 域名与备案 | 投放页无法外部访问 | 复用 nownexts.com 子路径（/h5/…）或 WebsFlow 现有托管 |
