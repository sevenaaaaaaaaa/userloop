# UserLoop Lessons（错误只犯一次）

> 与 OpenFlow/MFlow 一致的迭代纪律：每个真实缺陷都沉淀为一条 Lesson，下次不重犯。
> 本文件部分由 `userloop evolve lessons --write` 自动生成（来源 `evolution_lessons` 表），部分为人工整理。
> 最后更新：2026-09-18（v0.4.0）

## storage（存储与分层）

- **服务端 Store 未传配置 → 分层存储静默失效**
  - 现象：CLI 测试正常（走 MySQL），线上服务却一直写 SQLite；`db status` 显示 `sqlite` 而配置是 `mysql`
  - 原因：`create_app` 里 `Store(cfg["db_path"])` 漏传了 `cfg`
  - 处理：所有构造点统一传 `cfg`；启动日志打印 `events 后端：xxx`，一眼可见
  - 验证：服务器 `userloop db status` 显示 `mysql(3ms)`，且服务进程与 3307 有 ESTAB 连接

- **WAL 文件膨胀**
  - 现象：`userloop.db-wal` 涨到 4.1MB 不回落
  - 原因：autocheckpoint 只重置不缩容
  - 处理：`wal_autocheckpoint=512` + close/prune 时 `wal_checkpoint(TRUNCATE)`
  - 验证：清理后 WAL=0B

- **配置写入把中文写坏（截断 JSON）**
  - 现象：服务起不来，`JSONDecodeError`；config.json 被写了一半
  - 原因：服务器 python3.6 默认 ascii stdout/文件编码，`json.dump(ensure_ascii=False)` 中途崩
  - 处理：所有写配置脚本显式 `encoding="utf-8"`；关键配置改动走脚本而非内联 heredoc
  - 验证：`config rebuilt ... JSON OK` + 服务 active

- **多租户串库（最严重）**
  - 现象：新建租户 `acme` 的 overview 显示主租户的 2386 条事件
  - 原因：`tenant_config` 在新租户未声明 storage 时**回落到了基础配置**（主租户的 MySQL）
  - 处理：新租户绝不继承基础存储（`cfg["storage"] = tenant.get("storage") or {}`）+ 回归测试
  - 验证：main→`mysql(3ms)` / acme→`sqlite`，事件各 2386 / 1，零串库

## frequency（频控与触达）

- **模板 Loop 绕过频控**
  - 现象：AI 决策受频控，模板 Loop 的邮件/短信不受限
  - 原因：门禁只加在 AI 护栏层
  - 处理：把频控提升为 `execute_action` 包装层（模板/Canvas/AI/直连全覆盖）
  - 验证：同用户 5 秒内二次触达被拦（`blocked_by_frequency`）

- **频控依赖 actions 表 → Canvas/直连不落表即绕过**
  - 处理：独立触点台账 `touch_log`（所有投递统一记账）
  - 验证：Canvas 场景与直连调用均被计入并拦截

- **`touch.auto` 漏记账**
  - 现象：自动选渠道成功后不产生台账，后续触达不受频控
  - 原因：包装层拿到的 channel 是 `auto` 而非解析后的真实渠道
  - 处理：按 `result["channel"]`（解析后）记账

- **硬规则被频控抢先**
  - 现象：已退订用户被拦时的原因是"频控"，造成误判
  - 处理：退订/抑制等硬规则**优先于**频控判断，原因更准确

- **事务类消息占用营销配额**
  - 现象：订单收据也计入周上限，正常营销被挤掉
  - 处理：`transactional_templates` 白名单豁免（且不写营销台账）

- **配置被写成非法类型会破坏整条触达链**
  - 现象：自进化把 `global_gap_hours` 回滚成 `{}` → `hours_since < {}` 会 TypeError
  - 处理：频控 `cfg_of` 做类型防御（非法值忽略、回落默认）+ 回滚区分"缺失/空值"（缺失则删除该键）
  - 验证：`test_frequency_cfg_ignores_invalid_types`、`test_apply_rollback_when_key_absent_restores_default`

## ai（AI 与护栏）

- **冷却期内反复生成待批决策（审批队列被撑爆）**
  - 现象：64 条待批里 57 条是同一批用户在冷却期的重复项
  - 处理：决策前做冷却预检（`has_recent_loop(ai.<intent>)`），命中则记 `skipped` 不排队；`force` 可人工覆盖

- **周报 AI 叙事 400**
  - 现象：`HTTP 400`，叙事总是降级
  - 原因：`chat()` 强制 `response_format=json_object`，而周报要 Markdown（DeepSeek 要求 json 模式的提示含 "json"）
  - 处理：`chat(json_mode=True|False)` 可配；叙事类关闭 json 模式

- **Copilot 输出被 max_tokens 截断 → 非法 JSON**
  - 处理：结构化生成预算提到 1200 + 容错解析（去围栏/截取首尾大括号/夹杂解释可恢复）

- **AI 会编造不存在的枚举值**
  - 现象：Copilot 用了阶段 `paid`（我们只有 `paying`）
  - 处理：服务端强校验与裁剪（触发器/动作白名单/阶段/数值范围），并回显 warning；越权永不生效

## predict（预测）

- **`data_span_days` 把 `aiosqlite.Row` 当 dict → 恒为 0**
  - 现象：自适应窗口塌成 1 天，标签窗口错配 → 全为正例无法训练
  - 处理：正确解析 Row（`row["c"]`），并加单测
  - 验证：线上窗口正确显示 `5/5 天`，样本不足时**诚实回退**并说明原因

## tenant / i18n

- **调度路径 `ctx.store` 未挂载**
  - 现象：频控台账与投递回执静默失效（调度链路拿不到 store）
  - 处理：调度/服务/CLI 入口统一挂载；新增 `test_scheduler_path_logs_block_and_sent`

- **路由顺序冲突**
  - 现象：`GET /predictions/model` 返回 404（被 `/{user_id}` 抢先匹配）
  - 处理：静态路径路由必须先于参数路由注册

## 生态与接口（本轮 N1 新增）

- **inFlow 洞察有生命周期：`new → acknowledged`**
  - 现象：第一次同步 10 条后再同步又"新建"10 条
  - 原因：我们对每条洞察回执 `ack`，它们已离开 `new` 状态；下一批是新的 new 洞察
  - 结论：这是**正确行为**（回执通道有效），但同步要按 `external_id` 去重（已做，`UNIQUE(source, external_id)`）

- **内容归因计数查错了库**
  - 现象：窗口内注入 8 条命中事件，`hits=0`、verdict 恒 neutral
  - 原因：`count_events_matching` 用 `store.db`（SQLite），而事件在 **MySQL 事件后端**
  - 处理：计数下沉到 EventStore 接口（SQLite/MySQL 双实现），store 统一委托
  - 验证：命中数 8（等于注入数）→ verdict `effective`

- **同一事件命中多个归因信号会重复计数**
  - 现象：`hits=16`（8 条事件 × 2 个信号 utm_campaign + url）
  - 处理：改为 `count_matching_any`（`props LIKE a OR props LIKE b`，按事件去重）
  - 验证：`test_attribution_counts_each_event_once`

- **入站端点必须在免登录白名单内（否则被自家门禁先拦）**
  - 现象：MFlow 发布适配器调用 `/hub/mflow/publish` 返回 403/401，日志看不到进入 handler
  - 原因：该路径不在 `public_api`，请求先被鉴权中间件拦下，根本没走到 handler 自带的 token 校验
  - 处理：把"自带 token 校验的入站端点"加入白名单（ingest/track/**hub/publish**）；加测试断言 401→校验通过
  - 教训：**新增入站端点时，白名单与处理器校验必须一起改**

- **Cloudflare 会拦服务端到服务端的非浏览器 UA**
  - 现象：MFlow 的 `webhook.py`（`urllib`，默认 UA `Python-urllib/3.6`）经公网域名调用被 CF 403
  - 处理：同机互调改走**内部源站**（`http://127.0.0.1:8600/userloop/...`），仍是标准 HTTP API，且更快更稳
  - 原则：**跨系统同机调用优先内网地址**，公网地址留给外部系统

- **验证脚本自身也会踩"查错库"的坑**
  - 现象：核对回传事件时用 `store.db.execute("SELECT … FROM events")` → 0 条，误判为"没回流"
  - 原因：events 在事件后端（MySQL），`store.db` 是 SQLite 兜底表
  - 处理：核对一律走 `store.events.*` / `store.list_events()`；这条教训已在 Team 内重复出现两次（内容归因、回传核对）

- **桥接插件用 `X-UserLoop-Bridge` 鉴权，不是 `Authorization: Bearer`**
  - 现象：`openflow.automation` 动作返回 400（`openflow.automation: {"ok": false, "status_code": 400}`），而直连 curl 相同路由 200
  - 原因：适配器发 `Authorization: Bearer`，而桥接的 `$guard()` 只认 `X-UserLoop-Bridge` → 鉴权失败
  - 处理：桥接相关调用统一带 `X-UserLoop-Bridge`（Bearer 仅作兼容）；失败时回传响应体内的 message（否则只看到 400 无原因）
  - 教训：**跨系统鉴权头必须与对方 plugin 的 guard 实现对齐**，并且适配器必须回传对方错误信息

- **核对数据时再一次查错库（第 3 次）**
  - 现象：验证"OpenFlow 是否收到事件"时直接查 `openflow.db`（SQLite）→ 0 条，差点误判
  - 原因：OpenFlow 的 events 已迁到 MySQL（其 EventStore 双驱动），SQLite 只是兜底
  - 正确做法：**用对方的"功能证据"而非自己猜存储** —— 本次用桥接日志 `triggers:["automation","canvas"]` 证明链路生效
  - 已第三次踩到同类坑 → 建议：把"验证要查后端而非兜底库"写进团队代码规范

- **配方动作必须在白名单内**
  - 现象：inFlow 配方里的 `notify_team` 被 Copilot 校验丢弃成 `ai.compose`
  - 处理：统一用白名单内的 `feishu`（团队通知）/`touch.*`/`mflow.*`/`openflow.*`
  - 结论：**AI/配置产出的动作都要过白名单**，否则会被静默改写

## 生态与接口

- **OpenFlow 插件钩子用错 API**
  - 现象：`cdp_event_received` 是 filter，用了 `$p->on()`（action）→ 永不触发
  - 处理：改用 `$p->filter()` 并无条件透传；同时补 `http/log/config` 权限（fail-closed 会静默拦截）

- **插件配置目录不是代码目录**
  - 现象：`$p->get()` 恒为空 → 桥接 401/URL 非法
  - 原因：`PluginSDK::dir()` 指向 `data/plugins/<id>/`，配置写到了 `plugins/<id>/`
  - 处理：配置迁移到数据目录 + `www` 属主

- **MFlow 无密码头机制 / `loop/create` 必须带 `item_id`**
  - 处理：改为 `login` + session cookie（401 自动重登）；item_id 自动派生（`ul-<模板>-<尾号>-<时间>`）

- **不该干预对方状态机**
  - 现象：多调 `item/advance` 会与 MFlow 自身的推进冲突
  - 处理：只 `item/upsert` + `loop/create`，状态机交给 MFlow（有测试断言"不调用 advance"）

## 运维

- **Cloudflare 会缓存重定向与 HTML（含登录页）**
  - 现象：登录后跳到 `https://127.0.0.1:8600/userloop`；控制台登录页被 CDN 缓存
  - 原因：zone 级缓存规则带 `edge_ttl override`（无视源站 no-store，只豁免 PHPSESSID）
  - 处理：源站全部动态响应 `Cache-Control: no-store`；CF 缓存规则对 `/userloop*` 显式 bypass；禁用后端自动重定向（redirect_slashes=False）

- **sync.sh 验收脚本用系统 python 读含中文配置 → ascii 崩**
  - 处理：改用 venv python 读配置；启动就绪等待（避免 `activating` 中断发版）

- **服务器 python3.6 的 ascii stdout 是常见坑**
  - 处理：脚本输出避免中文 print，或显式 `encoding="utf-8"` 写文件
