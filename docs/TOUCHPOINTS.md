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
| **P0（已完成）** | 触点抽象 `userloop/touch/` + 邮件驱动（OpenFlow 桥 + SMTP 兜底）+ HTML 模板 + 自持追踪/退订 + 身份表 + OpenFlow 桥接插件 | ✅ 真实 HTML 邮件送达（250 Ok）、打开/点击/退订回流为旅程事件、退订进抑制名单且再发被拦截 |
| **P1（本轮已完成）** | 短信直连（阿里云/腾讯云/通用网关，含合规退订指令）+ H5 自持动态页（签名 URL + 打开/点击追踪回流） | ✅ 短信链路端到端验证（网关回声收到带「回T退订」的正文）；H5 页线上可打开、CTA 302、`h5_view`/`h5_click` 回流 |
| **P1（待凭据）** | 阿里云/腾讯云短信**正式发送**；公众号模板消息；企微应用消息 | 需：短信签名/模板报备 + AK/SK；公众号认证服务号（模板消息）；企微自建应用（secret） |
| **P2（A/B 已完成）** | **A/B 版式实验**：稳定分桶 → 内容槽位覆盖 → 打开/点击/转化按变体归因 → 双比例 z 检验 → winner 提升（写 feedback） | ✅ 线上实测：40 人 20/20 分桶、B 组 click_rate 0.5 vs A 0.2、置信 0.9533 判 winner、提升后新用户恒发 winner |
| **P2（待做）** | WebsFlow 富页面构建器（模块化/千人千面投放页）接入 `touch.h5` 富页面模式；企微 1v1/客户群 | 富页面链接可投放且事件回流 |
| **P3** | 内容资产中心（模板版本化/审批/预览）、发件域预热与配额治理、多语言触点 | 模板变更可灰度；发件域预热曲线可控 |

### P2 A/B 实验细节（2026-09-17）

- **稳定分桶**：`hash(experiment_id + user_id)` 落桶，同一用户始终同一版式（体验不抖动）
- **变体只覆盖内容槽位**（`cta_text` / `title` / `title_suffix` / `body_prefix|suffix`），版式由渲染层保证 —— AI/运营改文案不会把邮件改丑
- **指标归因**：分母=分桶人数；分子=窗口内事件去重人数（打开=email_open/h5_view，点击=email_click/h5_click，转化=实验 goal_event，复用现有验证回流）
- **显著性**：双比例 z 检验（双侧），达 `confidence` 阈值且双方样本 ≥ `min_samples` 才判 `winner`，否则 `running`/`insufficient`
- **闭环**：`promote` 提升 winner → 之后只发 winner，并写 feedback（`experiment.<id>`）供 AI 大脑参考
- **配置**：内置两个实验（邮件 CTA 紧迫感 / H5 标题风格），可经 `data/experiments.json` 覆盖扩展；控制台「A/B 实验」面板可视化 + 一键提升
- 实测：`h5_hero_style` winner=B（行动导向）置信 0.9533，已提升；邮件实验随自然触达累积样本

### P1 交付细节（2026-09-17）

- **短信驱动** `userloop/touch/sms_providers.py`
  - 阿里云 Dysmsapi（RPC HMAC-SHA1 签名）/ 腾讯云 SMS（TC3-HMAC-SHA256）/ 通用 webhook 网关
  - 合规：自动附加「回T退订」；`sms_unsubscribed` 用户永久拦截；未配凭据时**明确报错**（不假装成功）
  - 配置：`touch.sms.{enabled, provider, sign_name, template_code, access_key_id/secret}`（现值 `enabled=false`，填好即生效）
- **H5 触点**（自持，零外部依赖）
  - 交付即生成页面记录，返回公开链接 `/t/p/<page_id>?t=<签名>`
  - 打开记 `h5_view`、CTA 点击记 `h5_click`（含 page_id/loop_id/template_id）并 302 到目标；页面计数落 `touch_pages`
  - WebsFlow 富页面（模块化/千人千面）留 P2，通过同一 `touch.h5` 驱动的富页面模式接入


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
