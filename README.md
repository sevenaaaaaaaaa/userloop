<div align="center">

# UserLoop —— 独立产品人与小团队的全域运营中枢：让数据基建开始变现

**CDP 存了人、BI 出了报表、MA 能群发——三套系统谁也不认识谁。UserLoop 把它们装进一个引擎，让 AI Agent 替你把数据变成动作、把动作变成收入。**

![Language](https://img.shields.io/badge/Language-Python%203.12%2B-blue)
![Version](https://img.shields.io/badge/Version-0.6.1-green)
![Tests](https://img.shields.io/badge/%E6%B5%8B%E8%AF%95-209-brightgreen)
![License](https://img.shields.io/badge/License-MIT-blue)

[官网](https://nownexts.com) · [使用指南](docs/USAGE-GUIDE.md) · [功能总附录](docs/APPENDIX-FEATURES.md) · [相关仓库](https://github.com/sevenaaaaaaaaa)

</div>

---

## 这是什么

多数团队的困境不是没有数据，而是数据基建的花费迟迟变不成钱：CDP 把用户档案建起来了，却没人接着做动作；BI 把洞察写在报表里，洞察落不了地；MA 能发邮件发短信，却说不清该发给谁、发了有没有用。三套系统各管一段，接缝处全靠人肉导表——「数据驱动」喊了十年，在实际操作中始终无法有效发力。

UserLoop 就是为打破这个困境造的：把 CDP 的数据打通整合，让 BI 本该做好的用户洞察与细分动作，接上完善的 MA 触达能力，全部装进一个引擎。事件进来自动建档、判定旅程阶段、检测断点（注册没激活、加购没付款、老客户沉寂）；AI 大脑在护栏内决定每个用户的下一步最佳动作，或按模板触发邮件 / 飞书 / 优惠券 / Webhook；每个动作带目标事件与验证窗口，到期自动判定 effective 还是 neutral，结果回流更新档案。触达不再靠「想起来发一下」，效果不再靠猜。

AI Agent 是让这条路跑通的关键：自然语言一句话生成分群规则与旅程草稿，Agent 团队按角色拆解运营目标，风险动作一律回到人工审批门。规模已验证：8 阶段旅程引擎、5 个内置 Loop 模板、Canvas 可视化编排、A/B 实验、Prometheus 指标与备份告警，209 项自动化测试托底。

## 核心能力

- **全域数据接入（自收数）** —— `track.js` 一行嵌入收自家站点行为；GA4 / Segment / Shopify / HubSpot 报文直接喂进 `/api/v1/hub/ingest`，格式自动识别，原始报文落盘可回放，不租任何外部 CDP。
- **自有 CDP（自建档）** —— find-or-create 建档、匿名 / 实名身份自动合并、多租户隔离；一人一册：来源、事件、触达、验证结果一条时间线。
- **旅程引擎与断点检测** —— 访客到推荐者 8 阶段自动判定与迁移记录；进入新阶段、滞留超时、目标事件未达成三类断点实时检测。
- **Loop 引擎 + Canvas 编排** —— 断点命中模板自动生成 LoopRun（pending → running → verifying → verified/failed），内置 5 模板可 JSON 扩展；trigger / condition / action / delay / exit 五类节点画布编排，测试触发、执行留痕。
- **多通道触达** —— 邮件（SMTP 真发）、飞书、任意 MA Webhook、HubSpot lifecycle 同步；跨渠道全局频控、发前质检门拦截打扰；AI 按旅程上下文写文案，没配 key 自动降级模板。
- **洞察与预测** —— 自然语言分群（一句话出规则 JSON，服务端强校验）；churn 风险 / LTV 分层 / 购买倾向的可解释规则模型，每个分数带归因。
- **AI 大脑与 Agent 团队** —— AI 按阶段、行为、历史效果决定 Next Best Action，低风险自动执行、中风险进审批门、高风险阻断；Copilot 自然语言建流程（只出草稿，人审后启用）；Agent 按角色拆解目标执行。
- **自验效果 + A/B 实验** —— 每个动作带目标事件与验证窗口，到期自动判定并回流；版式 A/B 稳定分桶、双比例 z 检验定赢家、一键 promote。

## 真实界面

以下截图来自本地运行的真实控制台（`userloop demo` 灌入模拟数据后截取）：

**① 运营总览：漏斗、Loop 状态、验证回流一屏看完** —— 用户数、事件数、Loop 数、effective/neutral 统计和付费转化在首屏；旅程漏斗直接标出每个阶段的人数与占比，断点在哪一目了然。

![运营总览](docs/screenshots/01-console.png)

**② 画布编排：旅程画出来，执行全程留痕** —— trigger → condition → action → delay → exit 五类节点，BFS 自动分层布局，Flow JSON 编辑后保存生效，测试触发即时可跑。

![画布编排](docs/screenshots/02-canvas.png)

**③ 智能分群：一句话圈人** —— 输入「最近 7 天加购未付且 churn 分高于 0.5 的付费用户」，生成可执行的规则 JSON；churn / LTV 分层按人算好，命中数实时计算。

![智能分群](docs/screenshots/03-segments.png)

**④ Loop 运行：每个触达有结论** —— 模板、触发原因、状态（running / verified / skipped）、动作通道一行看清；下方 CDP 档案按人列出阶段、购买、事件数，验证回流直接改档案。

![Loop 运行](docs/screenshots/04-loops.png)

## 具体用例

**加购放弃挽回（独立电商 / 知识付费）** —— 启用内置 `cart_abandon` 模板：加购 2 小时未购买 → 优惠券 Webhook 触达；目标事件 purchase、验证窗口 48 小时，到期自动判定；想改话术去 Canvas 编辑动作节点，AI 文案按旅程上下文生成。结果：挽回不靠想起来，每波触达都有 effective / neutral 结论，跑两周看数据再决定加码还是换打法。

**多平台数据打通（同时用 Shopify / HubSpot / 自建站的人）** —— 自建站贴 `track.js`，Shopify / HubSpot 报文指向 `/api/v1/hub/ingest`（配 HMAC），格式自动识别归一。结果：同一个人在不同平台的行为进同一册档案，旅程判定与 Loop 触发在完整数据上跑，不再各平台各看各的。

**流失救援（订阅制产品）** —— 启用 `churn_risk_rescue` 模板：激活用户 14 天无事件 → 飞书先预警给你，召回邮件同时发给用户；验证窗口 7 天内出现任意事件即算救回，结果回流档案。结果：流失在发生前被拦下，而不是月报里才看见。

## 快速开始

要求 Python 3.12+，无 MySQL / Redis 依赖，SQLite 开箱即用：

```bash
git clone https://github.com/sevenaaaaaaaaa/userloop.git && cd userloop
pip install -e .

userloop init     # 初始化工作区
userloop demo     # 端到端演示：模拟事件 → 自动 Loop → 执行 → 验证回流
userloop serve    # 控制台 http://localhost:8600（默认免登录，见 auth.json 说明）
```

打开控制台先看 demo 生成的 Loop 运行与验证结果——这一步不需要接任何外部系统。然后开始收真实事件：

```html
<script src="http://your-userloop-host/track.js"></script>
```

或直接喂事件：

```bash
curl -X POST http://localhost:8600/api/v1/ingest \
  -H 'Content-Type: application/json' \
  -d '{"distinct_id": "user_42", "email": "u42@example.com", "event": "purchase", "props": {"amount": 299}, "source": "web"}'
```

运维：`userloop ops health`（深度健康检查）· `GET /api/v1/metrics`（Prometheus 文本 / JSON）· 外部 AI 接入 `userloop mcp`。完整功能清单见[功能总附录](docs/APPENDIX-FEATURES.md)。

## 开源开放

UserLoop 的核心功能——数据接入、CDP、旅程、Loop、触达、分群、预测、A/B、控制台——**过去、现在、将来都持续开源**，MIT 协议。

商业化被认定为定制化开发项目：私有部署服务、定制集成、行业资产包定制，属于服务合同，与开源版无关。我们同时支持包括 OpenFlow 在内的开源生态，`plugins/` 目录与插件注册表（source / action / model / template 四类）向所有开发者开放——欢迎按需快速开发插件完善体验，fork 出自己的版本参与市场竞争。

## 与 OpenFlow 的关系

UserLoop 是产品矩阵中的**进阶层单件**：当你在用 OpenFlow 的过程中长出「全域数据打通、跨系统旅程编排」的需求时，装上它；不装 OpenFlow，它也完整成立——单独用就是一套「接入 → 建档 → 旅程 → 触达 → 验证」的闭环。

- 已在用 OpenFlow → 站内行为经其插件（`userloop-tracker`）旁路流入，高价值信号经 HMAC 回推 OpenFlow CDP，被 GrowthBrain 消费（已实测上线）；
- 已在用 MFlow → 旅程断点变成内容选题（只创建草稿，绝不代发布）；
- 已在用 HubSpot 等外部 MA → lifecycle 自动同步，任意 MA 走带 HMAC 的 Webhook。

**账号互通**：登录体系与 OpenFlow / MFlow 同源（auth.json 同名同密码），支持 `@userloop` 专属后缀，一套账号进矩阵。联动是插件式组合，不是依赖——四通道全走公开 API，任何一路拔掉，闭环不中断。

## 当前边界（诚实声明）

- 广告 / 社媒平台级拉取适配器（Meta / Google Ads API 直连拉取）未做：GA4 MP / Segment / Webhook 等格式层已收，平台拉取属路线图；
- AI 个性化触达需自配 OpenAI 兼容 key（默认 DeepSeek）；未配置时自动降级为模板文案，闭环不受影响；
- 预测层 v1 是可解释规则模型，不是训练模型——样本量与可解释性优先，特征与标签已在为后续拟合沉淀；
- 不做站内 CMS / 课程 / 商店——这些是矩阵其他产品的领域，UserLoop 只做数据、旅程与触达，不吞并；
- MCP / API 可写，但写操作一律经自己的状态机 + 审批门；不代写外部 MA 的敏感操作。

我们区分**已实现 / 已接入 / 已被使用 / 已验证有效**，不把远景写成现状。

## License

MIT（声明见 `pyproject.toml`）。
