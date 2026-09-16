# UserLoop

**全域自动化用户运营工具 —— 让全用户旅程自动形成 Loop。**

UserLoop 监听全域用户行为事件，自动为每个用户重建生命周期旅程（Visitor → Signup → Activated → Paying → Retained → Advocate），在旅程断点（卡点、流失、机会）处**自动生成运营 Loop**：触发 → 动作（邮件/Webhook/飞书/连接器）→ 目标验证 → 效果回流，无需人工逐条搭建自动化。

> 借鉴家族项目：OpenFlow（事件总线 + CDP + 流程执行三件套）、MFlow（状态机 SSOT + 质量钩子 + 调度）、inFlow（动作派发 → 验证窗口 → 回流闭环范式）。

## 核心闭环

```
全域事件 (web/app/邮件/API)
   │  POST /api/v1/ingest
   ▼
事件总线 bus.handle()  ←—— 对齐 OpenFlow flow_handle()
   ├─ CDP 建档：find-or-create 用户，合并匿名/实名身份
   ├─ 旅程引擎 journey.evaluate()：阶段判定 + 阶段迁移记录
   ├─ 断点检测：进入新阶段 / 滞留超时 / 目标事件未达成
   ▼
Loop 引擎 loops.evaluate()
   ├─ 匹配 Loop 模板（触发条件 + 动作序列 + 目标 + 验证窗口）
   ├─ 生成 LoopRun（状态机：pending→running→verifying→verified/failed）
   ▼
动作执行器 executors（webhook / email / feishu / generic）
   ▼
调度器 scheduler：延迟动作 + 验证窗口到期检查
   ▼
验证 verify：目标窗口内出现目标事件 → verdict(effective/neutral)
   ▼
feedback 回流 → 更新用户画像 / 模板统计
```

## 快速开始

```bash
# 安装
cd "UserLoop Dev" && pip install -e ".[dev]"

# 初始化工作区
userloop init

# 端到端演示：模拟一批用户旅程事件 → 自动生成 Loop → 执行 → 验证回流
userloop demo

# 启动服务 + 控制台 (http://localhost:8600)
userloop serve
```

## 接入事件

```bash
curl -X POST http://localhost:8600/api/v1/ingest \
  -H 'Content-Type: application/json' \
  -d '{
    "distinct_id": "user_42",
    "email": "u42@example.com",
    "event": "purchase",
    "props": {"amount": 299},
    "source": "web"
  }'
```

## 目录

```
userloop/
├── core/
│   ├── entities.py    # Pydantic 实体
│   ├── store.py       # SQLite 存储层（WAL）
│   ├── bus.py         # 事件总线（唯一入口）
│   ├── journey.py     # 旅程引擎：阶段判定 + 断点
│   ├── loops.py       # Loop 引擎：模板匹配 → LoopRun
│   ├── executors.py   # 动作执行器
│   ├── verify.py      # 目标验证 + 回流
│   └── scheduler.py   # APScheduler 调度
├── server/app.py      # FastAPI（ingest/dashboard/users/loops API）
├── web/index.html     # 控制台
└── cli.py             # serve / init / demo
data/                  # 运行时数据（SQLite + 模板配置）
```

## Loop 模板

模板是 JSON（`data/loop-templates.json`），字段：`trigger`（事件/滞留/进入阶段）、`actions`、`goal_event`、`verify_window_hours`、`cooldown_hours`。内置示例：

| 模板 | 触发 | 动作 | 目标 |
|---|---|---|---|
| signup_no_activate | 注册后 24h 未激活 | 邮件引导 | activation (72h) |
| cart_abandon | 加购 2h 未购买 | 优惠券 webhook | purchase (48h) |
| first_purchase_celebrate | 进入 paying 阶段 | 新客欢迎 | 无验证 |
| churn_risk_rescue | 激活用户 14d 无事件 | 飞书预警 + 邮件召回 | 任意事件 (7d) |
| advocate_prompt | 进入 advocate 阶段 | 推荐邀请邮件 | referral (14d) |

## 生态 Action 适配器（OpenFlow / MFlow）

对齐 inFlow `docs/04` §4 动作路由器映射表，适配器实现统一接口 `execute(action, ctx) -> {ok, ref, detail}`，失败不阻塞闭环：

| 动作类型 | 目标 | 调用 |
|---|---|---|
| `ai.email` | DeepSeek | LLM 按旅程上下文生成 `{subject, text}` → SMTP 真实发送；未配 key 优雅降级模板文案 |
| `ai.compose` | DeepSeek | LLM 生成文案仅落 outbox 审计（人工审阅/下游消费） |
| `openflow.webhook_insight` | OpenFlow | `POST /api/webhook.php`（InboundReceiver，`X-Inbound-Signature` = HMAC-SHA256） |
| `openflow.automation` | OpenFlow | 连接器式 POST（Bearer 鉴权） |
| `openflow.plugin_api` | OpenFlow | `POST /api/plugin/userloop/{method}` |
| `mflow.create_content` | MFlow | `POST /api/loop/create {topic, brief, max_rounds}`（`X-MFlow-Password` 认证；只创建 Loop/草稿，绝不触发发布） |
| `mflow.register_topic` | MFlow | `POST /api/item/upsert` 登记选题进 12 阶段状态机 |

配置（`data/config.json`）：

```json
{
  "integrations": {
    "openflow": {"base_url": "https://your-openflow.com", "webhook_secret": "...", "inbound_id": "userloop"},
    "mflow": {"base_url": "http://127.0.0.1:8088", "password": "..."}
  },
  "ai": {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-...", "model": "deepseek-chat",
         "tone": "简洁友好，不夸张不堆砌"}
}
```

## 旅程埋点采集（web-tracker 插件）

任意站点一行嵌入，自动采集 `page_view` / `element_click`，批量 sendBeacon 上报（`plugins/web-tracker/`）：

```html
<script src="http://your-userloop-host/track.js"></script>
```

- 上报端点：`POST /api/v1/track`（批量 `{events:[...]}`，`event_id` 幂等去重，session_id 归档进 props）
- 事件走同一事件总线：CDP 建档 → 旅程阶段判定 → 断点 → Loop/Canvas

## Canvas 可视化编排

nodes/edges 图执行（对齐 OpenFlow CanvasSystem），控制台 `▦ 画布编排` 页可查看、编辑 JSON、测试触发：

```json
{
  "id": "welcome_canvas",
  "nodes": [
    {"id": "t1", "type": "trigger", "event": "signup"},
    {"id": "c1", "type": "condition", "rules": [{"field": "user.email", "op": "exists"}]},
    {"id": "a1", "type": "action", "action": {"type": "email", "payload": {"subject": "欢迎", "text": "hi {name}"}}},
    {"id": "d1", "type": "delay", "minutes": 1440},
    {"id": "x1", "type": "exit"}
  ],
  "edges": [{"from": "t1", "to": "c1"}, {"from": "c1", "to": "a1", "label": "true"}, {"from": "a1", "to": "d1"}]
}
```

节点类型：`trigger`（event / stage 触发）、`condition`（点路径求值 user./stats./props.，true/false 分支）、`action`（复用全部动作执行器含适配器）、`delay`（写等待队列，调度器到点恢复）、`exit`。执行留痕于 `canvas_runs.trace`。

## 与家族系统的关系

- **OpenFlow**：`openflow.*` 适配器把旅程信号推入其 InboundReceiver（落 CDP/线索）→ 被 GrowthBrain/画布/Agent 消费
- **MFlow**：`mflow.*` 适配器把旅程断点变成内容选题（`/api/loop/create` 异步产稿）
- **inFlow**：四通道设计的规范来源（`docs/04`），UserLoop 是"旅程→Loop"的执行侧实现
