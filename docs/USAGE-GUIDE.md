# UserLoop 使用指南

> 核心定位 · Features · 具体用例。写给"用户在漏斗里流失、触达全靠想起来"的独立产品人 / 运营,
> 也是官网改版的素材源(与本 README 同源维护)。

---

## 核心定位

**UserLoop 是独立的全域营销数据中枢 + 用户旅程 Loop 引擎:自收数、自建档、自触达、自验效果。它是进阶层单件——你在用 OpenFlow 后长出"跨系统旅程编排"需求时装它,不装 OpenFlow 它也完整成立。**

用户注册了没激活、加购了没付款、老客户 14 天没动静——这些断点每家公司都有,大多数团队的处理方式是"等运营想起来再说"。UserLoop 把断点变成自动闭环:检测到断点 → 按模板触发动作(邮件 / 飞书 / 优惠券 / Webhook)→ 验证窗口内看目标事件有没有发生 → 结果回流更新档案。断点不用人盯,触达不用人发,效果不用人猜。

它不是又一个 CRM 或营销云:数据自己收(埋点脚本一行嵌入,GA4 / Segment / Shopify / HubSpot 报文直接进,统一归进自己的档案,不租用外部 CDP);效果自己验(每个触达带目标事件和验证窗口,告诉你 effective 还是 neutral,而不是只给发送量和打开率);闭环自己转(OpenFlow、MFlow、HubSpot 都是可选的数据源或输出目标,拔掉任何一个,闭环不中断)。

## Features(按你将真实用到的顺序)

### 1. 第一天:先跑一遍 Demo 闭环
`userloop init` → `userloop demo`:模拟一批用户旅程事件 → 自动生成 Loop → 执行 → 验证回流,再打开控制台 `:8600` 看运行与验证结果。这一步不接任何外部系统。

### 2. 自收数:一行埋点 + 万能入站口
`<script src=".../track.js"></script>` 贴进站点开始收真实事件;`/api/v1/hub/ingest` 自动识别 GA4 / Segment / Shopify / HubSpot / 通用格式(HMAC 校验,原始报文落盘可回放)。

### 3. 自建档:档案在自己库里
find-or-create 用户、匿名 / 实名身份自动合并、多租户隔离。一人一册:来源、事件、触达、验证结果一条时间线。

### 4. 旅程引擎:8 阶段 + 三类断点
访客到推荐者阶段自动判定与迁移记录;进入新阶段、滞留超时、目标事件未达成,三类断点实时检测。

### 5. Loop 引擎:断点自动成闭环
内置 5 个模板(注册未激活引导 / 加购放弃券 / 首单祝贺 / 流失救援 / 推荐邀请),JSON 模板文件可自建扩展;LoopRun 状态机 pending → running → verifying → verified/failed,全程留痕。

### 6. 自触达:多通道 + AI 文案
邮件(SMTP 真发)、飞书、任意 MA Webhook、HubSpot lifecycle 同步;DeepSeek 按旅程上下文生成个性化文案,没配 key 自动降级模板,功能不断。

### 7. Canvas:把旅程画出来
trigger / condition / action / delay / exit 五类节点搭多步旅程,画布页可查看编辑、测试触发,执行全程留痕。

### 8. 自验效果:验证窗口 + A/B
每个动作带目标事件与验证窗口,到期自动判定 effective / neutral;版式 A/B 稳定分桶、双比例 z 检验定赢家、一键 promote,不需要另建指标体系。

## 具体用例(可直接照抄的实战路径)

### 用例 A · 多平台数据打通(同时用 Shopify / HubSpot / 自建站的人)
1. 自建站贴上 track.js 一行埋点,开始收页面行为;
2. Shopify / HubSpot 报文指向 `/api/v1/hub/ingest`(配 HMAC),格式自动识别归一;
3. 控制台看同一用户跨平台合并后的档案时间线;
4. 档案齐了,旅程判定与 Loop 触发直接在完整数据上跑。
**结果**:一个人在不同平台的行为进同一册档案,不再各平台各看各的。

### 用例 B · 加购放弃挽回(独立电商 / 知识付费)
1. 启用内置 `cart_abandon` 模板(加购 2h 未购买 → 优惠券 webhook);
2. 目标事件 purchase、验证窗口 48h,到期自动判定;
3. 想改话术:Canvas 里编辑动作节点,AI 文案按旅程上下文生成;
4. 跑两周看 verdict 统计,再决定加码还是换动作。
**结果**:挽回不靠"想起来发一下",每波触达都有 effective / neutral 结论。

### 用例 C · 跨 MA 人群同步(把 HubSpot 当主 MA 的团队)
1. UserLoop 判定完旅程阶段,`ma.hubspot.*` 自动同步 lifecycle;
2. 其他任意 MA 走带 HMAC 的通用 Webhook;
3. 高价值信号同时回推 OpenFlow CDP(可选,被 GrowthBrain 消费)。
**结果**:人群状态跨系统一致,不用手工导表对齐。

### 用例 D · 流失救援(订阅制产品)
1. 启用 `churn_risk_rescue` 模板(激活用户 14 天无事件);
2. 飞书先预警给你,召回邮件同时发给用户;
3. 验证窗口 7 天内出现任意事件,即算救回并回流档案。
**结果**:流失在发生前被拦下,而不是月报里才看见。

## 常见问题

**Q:能直连拉取 Meta / Google Ads 数据吗?**
平台级拉取适配器未做(属路线图);GA4 MP / Segment / Webhook 等格式层已收,先把报文喂进来即可。

**Q:AI 个性化文案必须配 key 吗?**
DeepSeek 等 OpenAI 兼容 key 可选;没配自动降级为模板文案,闭环不受影响。

**Q:它能替我建站 / 开商店吗?**
不做站内 CMS / 课程 / 商店——那些是矩阵其他产品的领域,UserLoop 只做旅程编排与触达,不吞并。

**Q:API / MCP 能直接改模板、发触达吗?**
写操作一律经自己的状态机与审批门,不直接改模板、不直接发触达;MCP 四工具(dashboard / journey / loops / feedback)只读回读。

**Q:必须配合 OpenFlow 吗?**
不必。单独用就是一套完整的"旅程 → 触达 → 验证"闭环;联动是插件式组合,四通道全走公开 API,任何一路拔掉闭环不中断。

**Q:报文出了问题能追溯吗?**
能。`/api/v1/hub/ingest` 的原始报文落盘可回放;运维侧有 `userloop ops health` 深度健康检查、Prometheus 指标(`GET /api/v1/metrics`)与确定性告警、备份。

## 进阶

- **配 OpenFlow**:站内行为经 `userloop-tracker` 插件旁路流入,高价值信号经 HMAC 回推 OpenFlow CDP(已实测上线)。
- **配 MFlow**:旅程断点经 `mflow.create_content` 变内容选题(只创建草稿,绝不代发布)。
- **配 PayFlow / LearnFlow**:订单与学习事件流入旅程引擎,付费、完课都能成为触发点。
- **让外部 AI 回读**:`userloop mcp` 启动 MCP Server,dashboard / journey / loops / feedback 四工具只读查数,你的 Agent 可直连。
- **下一步阅读**:[触达点位](TOUCHPOINTS.md) · [定位与边界自评](POSITIONING.md) · [运维手册](OPS.md) · [生态与演进](ECOSYSTEM-AND-EVOLUTION.md)。

---

*与 README 同源维护;功能口径以代码与 209 项自动化测试为准。*
