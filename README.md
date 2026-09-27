<div align="center">

# UserLoop

**独立全域营销数据中枢 + 用户旅程 Loop 引擎：自收数、自建档、自触达、自验效果**

![Language](https://img.shields.io/badge/Language-Python%203.12%2B-blue)
![Version](https://img.shields.io/badge/Version-0.6.1-green)
![Tests](https://img.shields.io/badge/%E6%B5%8B%E8%AF%95-209-brightgreen)
![License](https://img.shields.io/badge/License-MIT-blue)

[官网](https://nownexts.com) · [使用指南](docs/USAGE-GUIDE.md) · [相关仓库](https://github.com/sevenaaaaaaaaa)

</div>

## 这是什么

用户注册了没激活、加购了没付款、老客户 14 天没动静——这些断点每家公司都有，大多数团队的处理方式是「等运营想起来再说」。UserLoop 把用户旅程里的断点变成自动闭环：检测到断点 → 按模板触发动作（邮件 / 飞书 / 优惠券 / Webhook）→ 验证窗口内看目标事件有没有发生 → 结果回流更新档案。断点不用人盯，触达不用人发，效果不用人猜。

它不是又一个 CRM 或营销云。第一，数据自己收：埋点脚本一行嵌入，GA4 / Segment / Shopify / HubSpot 的报文也能直接进，统一归进自己的档案，不租用任何外部 CDP。第二，效果自己验：每个触达都有目标事件和验证窗口，跑完告诉你这波是 effective 还是 neutral，而不是只给你发送量和打开率。第三，闭环自己转：OpenFlow、MFlow、HubSpot 都是可选的数据源或输出目标，拔掉任何一个，闭环不中断。

已验证的能力规模：8 阶段旅程引擎 + 5 个内置 Loop 模板 + Canvas 可视化编排 + A/B 实验；209 项自动化测试；0.6.0 完成运维加固（深度健康检查、Prometheus 指标、确定性告警、备份）。

核心闭环：

```
全域事件 (web / app / 邮件 / API / Shopify / HubSpot)
   │  POST /api/v1/ingest 或 /api/v1/track 或 /api/v1/hub/ingest
   ▼
事件总线 bus.handle()
   ├─ CDP 建档：find-or-create 用户，合并匿名/实名身份
   ├─ 旅程引擎 journey.evaluate()：阶段判定 + 阶段迁移记录
   ├─ 断点检测：进入新阶段 / 滞留超时 / 目标事件未达成
   ▼
Loop 引擎 loops.evaluate()
   ├─ 匹配 Loop 模板（触发条件 + 动作序列 + 目标 + 验证窗口）
   └─ 生成 LoopRun（状态机：pending → running → verifying → verified/failed）
   ▼
动作执行器（email / feishu / webhook / HubSpot / DeepSeek AI 文案）
   ▼
验证 verify：目标窗口内出现目标事件 → verdict(effective / neutral)
   ▼
feedback 回流 → 更新用户画像 / 模板统计
```

## 核心能力

- **自收数：全域事件接入** —— `POST /api/v1/ingest`（单条/批量）、浏览器埋点 `track.js` 一行嵌入（`/api/v1/track` 批量上报、幂等去重）、`/api/v1/hub/ingest` 自动识别 GA4 / Segment / Shopify / HubSpot / 通用格式（HMAC 校验，原始报文落盘可回放）。
- **自建档：自有 CDP** —— find-or-create 用户、匿名/实名身份合并、多租户隔离，档案在自己库里，不依赖外部 CDP。
- **8 阶段旅程引擎** —— 访客到推荐者的阶段自动判定与迁移记录，三类断点实时检测：进入新阶段、滞留超时、目标事件未达成。
- **Loop 引擎：断点自动成闭环** —— 模板匹配生成 LoopRun（状态机 pending → running → verifying → verified/failed）；内置 5 个模板（注册未激活引导 / 加购放弃券 / 首单祝贺 / 流失救援 / 推荐邀请），JSON 模板文件可自建扩展。
- **自触达：多通道动作执行器** —— 邮件（SMTP 真发）、飞书、任意 MA Webhook、HubSpot lifecycle 同步；DeepSeek 按旅程上下文生成个性化文案，没配 key 自动降级模板，功能不断。
- **Canvas 可视化编排** —— trigger / condition / action / delay / exit 五类节点搭多步旅程，控制台画布页可查看编辑、测试触发，执行全程留痕。
- **自验效果：验证回流 + A/B 实验** —— 每个动作带目标事件与验证窗口，窗口到期自动判定；版式 A/B 稳定分桶、双比例 z 检验定赢家、一键 promote，不需要另建指标体系。
- **矩阵联动四通道（全部可选）** —— OpenFlow 站内事件流入、高价值信号回推 OpenFlow CDP、MFlow 旅程断点变选题（只创建草稿，绝不代发布）、MCP Server 只读回读（dashboard / journey / loops / feedback 四工具）。

## 快速上手

```bash
# 依赖：Python 3.12+
git clone https://github.com/sevenaaaaaaaaa/userloop.git && cd userloop
pip install -e ".[dev]"

userloop init     # 初始化工作区
userloop demo     # 端到端演示：模拟一批用户旅程事件 → 自动生成 Loop → 执行 → 验证回流
userloop serve    # 启动服务 + 控制台 http://localhost:8600
```

打开控制台 `http://localhost:8600`，先看 demo 生成的 Loop 运行与验证结果——这一步不需要接任何外部系统。然后把埋点脚本贴进你的站点开始收真实事件：

```html
<script src="http://your-userloop-host/track.js"></script>
```

或直接喂事件：

```bash
curl -X POST http://localhost:8600/api/v1/ingest \
  -H 'Content-Type: application/json' \
  -d '{"distinct_id": "user_42", "email": "u42@example.com", "event": "purchase", "props": {"amount": 299}, "source": "web"}'
```

运维命令：`userloop ops health`（深度健康检查）· 指标 `GET /api/v1/metrics`（Prometheus 文本/JSON）· MCP 接入 `userloop mcp`。

内置 Loop 模板（`data/loop-templates.json`，JSON 可自建扩展）：

| 模板 | 触发 | 动作 | 目标（验证窗口） |
|---|---|---|---|
| signup_no_activate | 注册后 24h 未激活 | 邮件引导 | activation（72h） |
| cart_abandon | 加购 2h 未购买 | 优惠券 webhook | purchase（48h） |
| first_purchase_celebrate | 进入 paying 阶段 | 新客欢迎 | 无验证 |
| churn_risk_rescue | 激活用户 14d 无事件 | 飞书预警 + 邮件召回 | 任意事件（7d） |
| advocate_prompt | 进入 advocate 阶段 | 推荐邀请邮件 | referral（14d） |

## 与 OpenFlow 的关系

UserLoop 属于进阶层：单独用就是一套完整的「旅程 → 触达 → 验证」闭环，不需要先装任何其他系统。

- 已在用 OpenFlow → 站内行为经其插件（`userloop-tracker`，`cdp_event_received` 钩子）旁路流入 UserLoop；高价值信号经 `openflow.webhook_insight`（HMAC）回推 OpenFlow CDP，被 GrowthBrain 消费——已实测上线。
- 已在用 MFlow → 旅程断点经 `mflow.create_content` 变成内容选题（异步产稿），纪律沿袭矩阵约定：只创建 Loop/草稿，发布永远停在人工授权。
- 已在用 HubSpot 等外部 MA → `ma.hubspot.*` 同步 lifecycle，任意 MA 走带 HMAC 的通用 Webhook。

联动是插件式组合，不是依赖：四通道全走公开 API，任何一路拔掉，闭环不中断。写操作纪律：MCP/API 的写动作一律回到自己的状态机与审批门，不直接改模板、不直接发触达。

## 使用指南

完整使用指南见 **[docs/USAGE-GUIDE.md](docs/USAGE-GUIDE.md)**。

深入阅读：定位与边界自评 `docs/POSITIONING.md` · 触达点位 `docs/TOUCHPOINTS.md` · 运维手册 `docs/OPS.md` · 生态与演进 `docs/ECOSYSTEM-AND-EVOLUTION.md` · AI 原生路线 `docs/AI-NATIVE-ROADMAP.md` · 变更日志 `CHANGELOG.md`。

## 当前边界

- 广告/社媒平台级拉取适配器（Meta / Google Ads API 直连拉取）未做：GA4 MP / Segment / Webhook 等格式层已收，平台拉取属路线图。
- AI 个性化触达需自配 OpenAI 兼容 key（默认 DeepSeek）；未配置时自动降级为模板文案，闭环不受影响。
- 不做站内 CMS / 课程 / 商店——这些是矩阵其他产品的领域，UserLoop 只做旅程编排与触达，不吞并。
- MCP / OpenAPI 可写，但写操作一律经自己的状态机 + 审批门；不代写外部 MA 的敏感操作。

## License

MIT（声明见 `pyproject.toml`）。
