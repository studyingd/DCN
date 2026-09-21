# AGENTS.md — 助手工作约定（项目记忆）

## 什么时候写进本文件（2026-09-14 用户确认此习惯）

不是每次修完 bug 都写，只写**以后会被自己或别的会话再踩一遍**的知识：

- 反直觉的根因（不看规范/源码根本猜不到的，如协议长度前缀按字符数）
- 跨模块契约（多处共用的 ID 公式、字段语义、判定口径）
- **已删除**的分支/字段/入口，注明不要再加回来
- 有测试钉住的行为细节不写，测试比文档可靠；一次性排查过程不写

段落稳定且长期无回退风险时可下沉到 `docs/`，本文件只留「禁止回退」清单。

## 防"中途中断"（最高优先）

背景：2026-09-11 的会话多次出现单条回复被截断 / 陷入重复工具调用（同一段 invoke
重复多次后断掉），导致**已计划但未执行的编辑丢失**（`_notify_flag` 修复、#14 补发、
AGENTS.md 本文件都曾因此丢失重做）。用户明确要求避免并写入记忆。

约定（每次会话必须遵守）：

1. **能自测就自测，不要让用户手动测试**（2026-09-11 用户明确要求）。排查 RDP/
   上传等链路问题时，优先自己写脚本经真实 WS 桥复现（参考 `backend/_bench_ws.py`：
   先 `POST /api/auth/login` 拿 cookie，再 `POST /api/terminal/ticket` 签票，再连
   `ws://127.0.0.1:8004/ws/terminal/{id}?ticket=...`）。**ticket 必须在后端进程内
   签发**——`ws_ticket._redis` 常为 None（走进程内存），独立脚本里 `issue_ticket`
   后端看不到，会 4001。admin 密码在 `.env` 的 `ADMIN_PASSWORD`。
2. **单条回复保持短**：散文精简；一个 turn 内工具调用 ≤ 2 个，绝不重复发送同一个调用。
3. **长任务拆多个 turn**：每 turn 以一句结论收尾作 checkpoint，下一步留给下一个 turn。
4. **关键编辑后立即验证落盘**（grep / read 确认），防止截断丢编辑而不自知。
5. **一旦发现输出开始重复同一段内容，立即停止**，改用单条合并命令或拆 turn。
6. 汇报先给结论再给证据；证据用紧凑代码块/表格，不铺陈。
7. **代码改动必须同步进容器**（2026-09-14 用户明确要求）：每次前端/后端改动
   交付前，除本地门禁外还要重建对应容器并验证产物。compose 文件在 `docker/`
   目录且必须带 env 文件：`cd docker && docker compose --env-file ../.env up -d
   --build frontend`（后端同理 `--build backend`）。验证方式：grep 容器内
   `/usr/share/nginx/html/assets/` 新文案或对比构建产物 hash。

## 本项目通知模型现状（防重蹈覆辙）

- 唯一 webhook 事件：`automation.notify`（旧 `inspection.report` / `inspection.failed` /
  `job.failed` 已合并；`device.offline` / `device.online` 发射点已撤，主机离线走告警规则
  `host_status`）。
- **巡检报告附件不做“全有或全无”**（2026-09-20 修，勿回退）：`feishu.deliver_report`
  曾是任一接收人摘要失败就整单跳过 PDF 附件——一个不在应用可用范围的人会吞掉
  所有能收到的人的附件（实测：两接收人一人 230013，另一人只剩文字摘要）。
  现口径：只有摘要**一个都没送达**才跳过附件；附件只发摘要已送达的接收人
  （到不了的人 file 消息同样到不了）；部分失败记入结果 error 与
  `summary partial failure` 日志。配套认知：**飞书通讯录里能选到人 ≠ 能给他发
  消息**——选人走 contact 读权限，发消息要求用户在自建应用「可用范围」内
  （否则 230013 "Bot has NO availability to this user"）；webhook「测试」按钮
  只走 deliver 文本路径，测不出上传/可用范围问题，别拿“测试成功”推断
  “附件能发”。
- **告警通知是单卡片模型**(2026-09-15 用户定调)：created 扣住直到归因终态
  (completed/failed)才放行，放行的卡片必带归因结论;**不要宽限/两段式**(曾做
  60s 宽限先发卡+alert.analysis 补发，被用户否掉)。仅看门狗
  `ALERT_ANALYSIS_HOLD_TIMEOUT`(180s) 防归因卡死吞告警:超时先放行干净卡片
  (running 永不进 message)，结论产出后 `alert.analysis` 补发仅作**兜底**(迁移
  0041 已给订阅 alert.created 的启用 hook 补订阅)。单卡片不慢的杠杆是归因步数
  预算 `ALERT_ANALYSIS_MAX_STEPS`(4) 与取证项锁定，**不要再引入宽限**。处置
  进度仍走 `alert.remediation`,但**只推终态**(succeeded/failed/skipped,
  2026-09-16 收敛:running/verifying 这类瞬时中间态几秒内被终态覆盖,逐个推卡
  会让一次离线告警两分钟连发五六张卡;进度仍可在告警中心 remediation_state
  实时看)。**host_status/container_status 是两步卡片模型**(2026-09-16 用户
  定调):离线/异常**立刻**发卡,详情带「正在触发自动拉起操作」(matched 分支
  的话术追加,条件 ALERT_AUTO_REMEDIATION);拉起成功推第二条
  「自动拉起操作成功，目标已恢复运行」。**不扣住通知**——container_status
  的归因扣住因此撤销,结论改走 alert.analysis 补发(`_update` 的归因进度也
  只推终态,running 不再推空卡)。容器卡片字段:对象=容器名,
  地址=宿主(服务器/虚机)名称或 IP(见 `_payload` is_container 分支)。
  **host_status 恢复卡(alert.resolved)有自动处置结论时只保留「自动处置」块**
  (2026-09-17 用户定调):详情块是 base+进度层层叠加的历史消息(离线话术已
  过时、结论与处置块重复),不再渲染;自然恢复(未处置)的恢复卡详情照旧
  (2026-09-18 用户纠偏:只对有处置结论的恢复卡收紧,**不要扩大到一律跳过**)。
  **business_status 恢复卡一律不渲染详情**(2026-09-18 用户定调):业务无
  自动处置,消息里的"异常明细"是触发时刻快照,恢复后原样重铺过时且误导。
  见 feishu.build_card/build_text 的 `resolved and (detail or business_status)` 分支。
  **归因类告警
  (cpu/mem/disk)连 `alert.resolved` 也不推(停用规则自动关停、指标回落
  恢复都不再发第二张卡;实测曾因 resolved 卡重铺同样详情+归因被用户打回)。抑制点在
  `alerts._notify_event_in_background` 的 `_RESOLVED_QUIET_METRICS`。
  **container_status 例外(2026-09-17 用户定调)**:容器与主机同为两步卡片
  模型,恢复正常推 resolved 卡(自动处置结论由它闭环);但强制收尾(规则
  停用/删除/对象移出范围,`_close_event` 写入 `force_closed` 标记)仍静默。
  配套:飞书卡片对 cpu/mem/disk 告警**详情段=归因结论本身**(不铺"[pve] X
  内存使用率…已超过阈值…"基础文案,字段区已够表达;无归因(虚拟机未配置
  运维接入/未完成)则不渲染详情段)。**容器告警的卡片详情段=状态明细**
  ("Exited (0)…"字段区表达不了,2026-09-17 用户定调),有归因结论时才升级
  为 AI 归因块——`container_status` 已从 `feishu._ANALYSIS_METRICS` 渲染
  集合移除,`build_card/build_text` 的 `conclusion_style` 分支承载该口径。
- **已删除的告警入口(不要加回来,2026-09-16 用户拍板)**：① 飞书卡片上的
  「已知晓」按钮(含 HMAC 免登录链接/ack-link 端点/AckView 页/中间件地址
  自动学习/ALERT_CARD_ACK_BASE_URL 配置)——降噪交互收敛到告警中心站内
  「已知晓」按钮(snooze,冷却重发抑制);② `POST /api/alerts/events/{id}/resolve`
  手动标记恢复端点+前端按钮——评估器只查 pending/open,指标还超着时手动恢复
  会在 30~60s 后被重建事件+重复通知(和评估器打架);恢复由数据驱动闭环
  (指标回落自动 resolved),系统级收尾走 close_events_* 系列。同理旧死端点
  `/api/alerts/devices`、`/api/alerts/targets` 也已删除(前端零调用)。
- **告警归因走单-pass**(2026-09-15)：`agent.run_focus_diagnosis` 跳过交互循环的
  LLM 选择轮——取证项按指标锁定后并行执行(Linux 复用同一条 SSH 连接;
  `_SshSession` 懒开连接由 `_open_lock` 串行)，docker_stats 优先复用容器采集器
  快照(`_container_snapshot_evidence`，>180s 过期回退现跑)，最后一一次 LLM 出结论。
  **结论轮不带 tools 字段**(`_chat` 哨兵 `_UNSET` 省略,不是传空数组——OpenAI 兼容
  接口会拒绝空数组):带工具时模型违反提示去调 tool → content 为空 → 报告变成
  占位符而事件仍标 completed。结论轮超时用 `ALERT_ANALYSIS_LLM_TIMEOUT`
  (默认 60s——实测结论轮 ~16s,留 4x 余量,取证+失败重试一轮仍稳在 180s
  扣住窗口内;docker 部署下 .env 不透传该变量,代码默认即容器值),
  别用报告轮的 180s,否则最坏路径(取证 35s+LLM 180s)会
  超过 HOLD_TIMEOUT 退化成两条消息。**交互 `_loop` 只服务手动诊断与自动化
  agent 任务**，路由开关是 `start_diagnosis/_execute_run` 的 `single_pass` 参数
  (有测试钉死)，别把单-pass 渗进交互路径。归因证据里还会拼平台自己的指标趋势
  (设备=DeviceMetricSample、虚机=pve_guest_status 内存历史环，见下)，零成本区分
  业务增长与突发泄漏。实测(SRM/WinRM):148s(无上限) → 41s(步数预算) → 37s(单-pass+
  简洁结论);剩余下限是推理型模型的结论轮(~30s)，再要快只能给 Agent 换
  非推理模型。
- **归因/诊断的公共设施**(2026-09-15)：
  * `ANALYSIS_METRICS` 现含 container_status(曾长期是 `_ANALYSIS_FOCUS` 里的死
    配置)，宿主身份统一按 device_id > remediation_json.target_id > resource_id
    负数解码，`_analysis_target` 一处解析;`alerts._rule_covers` 的 container_/
    host_ 分支必须排在 ANALYSIS_METRICS 分支之前，否则容器事件被当指标事件
    比容器 id → 误判出范围而误关告警(有测试钉住顺序)。
  * `pve_guest_status` 维护每个 guest 的内存历史环(120 样本≈1h，仅 leader 实例)，
    `agent._metrics_summary` 对 AgentTarget 经绑定按 IP 反查后出趋势——
    get_metrics 对虚机不再是恒定的「尚未接入」。
  * `_chat` 对 429/5xx/连接抖动指数退避重试(`AGENT_LLM_MAX_RETRIES`)，鉴权/
    参数 4xx 不重试;交互 `_loop` 一轮多个 tool_call 并行执行(`_dispatch_calls`,
    重复 key 去重,tool 消息按原顺序回填);诊断运行数由 `AGENT_MAX_CONCURRENT_RUNS`
    信号量排队;运行终态落库后 set 完成事件,`remediation._wait_for_run` 事件
    唤醒代替秒级轮询;LLM 用量记在 steps 尾部(`llm_usage` 步骤,无表结构变更)。
  * 磁盘归因 focus 必须含目录级 `disk_usage_top`(du,仅 Linux 注册表有，
    Windows 自动跳过);Windows `system_info` 用 CIM 不用 systeminfo(10~30s→1~2s)。
- **业务的存在性判定按 `Business` 本体,不要按成员表反查**(2026-09-18 修):
  业务合法成员有三类(服务器 business_servers/接口 business_interfaces/PVE 虚机
  business_pve_guests),评估器三类都监控。`alerts` 规则保存校验
  (`routers/alerts._validate_rule_targets`)与业务候选接口(`/api/alerts/businesses`)
  曾只查/join business_servers——纯接口/虚机业务被误报「告警规则包含不存在的
  业务」(实测业务 cs 只有接口+虚机,保存 404)。ACL 与业务列表
  (`/api/businesses`)同口径:按成员判定,服务器走 device ACL、虚机走
  `user_can_access_pve_vmid`、纯接口业务无资源 ACL 语义直接放行。
- **虚机告警的「地址」解析口径**(2026-09-17):运维接入绑定 ip(持久,跨停机)
  > QGA 实时地址(guest 停机时必然问不出——host_status 告警恰在停机时触发)
  > 最近已知 IP(`pve_guest_status._last_known_ip`,leader 的 IP 刷新循环
  `run_pve_guest_ip_refresh_loop` 在 guest 运行期间低频问 QGA 记下,仅内存、
  重启清空;闸门=存在启用中的 host_status 规则,agent 失败后退避 30min)。
  QGA-only 虚机(没保存过绑定行)的地址列靠这层兜底,不要再以为"配了 QGA
  停机后也能问出 IP"。`_guest_qga_ip` 成功时会顺手 record_guest_ip。
- **业务状态告警的明细与回溯**(2026-09-18):`evaluate_business_alerts` 的消息带
  异常明细——逐个点名失败成员(服务器 name(ip) 离线/虚拟机 name 离线或未运行/
  接口「name」探测结论,最多 3 条+计数),汇总计数只留"有事"信号。评估周期
  `BUSINESS_ALERT_INTERVAL` 默认 30s;资源项带 `sampled_at`(失败成员的最新
  新鲜证据时刻,探测 checked_at/PVE 快照 checked_at,过期不回溯),sustain 从
  真实失败时刻起算,不叠评估相位。设备侧无逐台时间戳→不回溯。
- **虚机 host_status 跟 30s 快照周期评估**(`evaluate_pve_guest_host_status`,
  2026-09-16):旧路径在 60s 指标周期里每轮对每个平台实时调 PVE API,虚机离线
  告警迟到 2~3 个周期(用户实测打回);新路径读 pve_guest_status 内存快照零
  API 成本,PVE 不可达时跳过该平台(不误触发也不收敛),`sampled_at=checked_at`
  回溯 sustain。评估入口在 `run_pve_guest_status_loop`。
- **告警触发校准**：`first_triggered_at` 回溯到条目 `sampled_at`(设备=采集完成时刻、
  虚机=PVE 快照 `checked_at`)，sustain 从真实越限时刻起算;虚机指标跟着 30s PVE
  快照周期评估(不等 60s 指标周期)，`evaluate_alerts` 入口加锁串行。归因步数
  预算 `ALERT_ANALYSIS_MAX_STEPS`(4)，超步数是归因超宽限的主因时先调它。
- **虚机磁盘告警数据源**(2026-09-16 接通,曾长期是"建了规则永远不触发"的死
  配置):PVE 快照只有 cpu/mem,`disk_max_pct` 由 `guest_fs_metrics` 低频采集
  循环(QGA `get-fsinfo` 优先、运维接入 SSH/WinRM 兑底,默认 300s 一轮,
  仅在存在启用中的 disk_max_pct 规则时才采集)写内存缓存,
  `_pve_metric_entries` 合成进评估条目;采集失败/缓存缺失=值缺失→评估跳过,
  **不误恢复既有事件**。sustain 回溯用 `disk_sampled_at`(fs 采集时刻),不是
  PVE 快照时刻——两者差最多 5 分钟,`_sampled_at(metric)` 按指标取。
- **通知投递的跨实例去重占位只看状态,不看时间戳**(`alerts._claim_notification`,
  2026-09-18 修):占位=条件 UPDATE `notification_status: pending→sent`(评估器对
  首次/冷却重发/恢复通知入队前都先置 pending,「pending」是唯一"该发了"信号)。
  **不要把规则冷却或 last_notified_at 掺进占位条件**——alert.resolved 是一次性
  生命周期通知,拿 created 卡的通知时间做冷却判断会把恢复卡吞掉(实测:created
  发出 4.5 分钟后恢复,冷却 5 分钟未满 → resolved 被拦 → 状态永远挂 pending,
  用户"拉起成功后收不到恢复消息")。冷却重发的时机判断归评估器内存,跨实例竞态
  由原子翻转兜住;`notify_event`/`_claim_notification` 不再有 cooldown_seconds
  参数,别加回来。
- 开关 `config_json.webhook_notify` 语义 = **默认开**：缺键 = 开，仅显式 `false` 关；
  旧键 `report_webhook` 兼容读取。旧前端不发送该键 → 也会推送
  （这是"开关开了却没发"反复出现的根因修复，见 `automation._notify_flag`）。
- 前端入口 **:5040**（vite dev 实时源码）。:18080 / `dcn-nginx-test` 已删除
  （曾是 `/tmp/ngxroot` 静态快照，易过期）；生产式验证用 :8080 容器，重建命令
  见上方约定第 7 条。
- **顶栏全局搜索走 `/api/search`（`routers/search.py`，2026-09-17 新增）**：旧实现只
  遍历 roomStore 的机房/机柜/设备树，虚机永远搜不到（用户实测打回）。现改为后端
  单端点分域返回：设备=名称/IP 模糊匹配（要求 `device:view`），虚机=名称 ∪ 绑定
  IP ∪ 最近已知 IP（要求 `pve:view`+`user_can_access_pve`，数据源
  `collect_pve_guest_candidates` 内存快照，勿改成直查 PVE API）。虚机结果定位
  协议是跨模块契约：DashboardView 设 `pveFocus={connectionId, vmid}` prop →
  PvePanel 切连接/等 overview 加载后 openDetail 并回发 `focused` 事件让父级清空；
  vmid 跨平台不唯一，判定列表归属用 `guestsConnId`（loadAll 提交时记录），别按
  vmid 直接在旧列表里找。选中设备结果则统一切到 scene 面板再 openRackById
  （旧实现不切面板，非 scene 页选中时 openRackById 静默失败）。
- 巡检报告 PDF 字体：Docker 镜像走 wqy-microhei 内嵌；开发环境 CID 回退
  （飞书桌面端实测渲染正常）。
- **PDF 报告已删「异常主机明细」段**（2026-09-20 用户拍板，勿加回）：逐项
  明细与「异常清单」重复，删后报告只留 横幅 / 异常清单 / 全部主机概览 /
  尾注；主机级失败(无 item)的行在异常清单里已有兑底，别以为删段漏了它。
  全部主机概览「系统」列短名缩字号单行显示（`_fit_font_size`，实测
  wqy 9pt 下 'Windows Server' 宽 65pt 超出 26mm 列的可用 63.5pt，尾字符
  折行难看；缩到 8.5pt 单行），不要再回去加宽列。
- sshd 握手噪声（`kex_exchange_identification` / `banner line contains invalid
  characters`）已从「异常日志」计数剔除（方案 A），原始输出保留在 details.output，
  被滤条数记在 details.filtered_noise。同类已剔除：pvedaemon 的
  `qmp command 'guest-ping' failed` 超时——是「guest 没装 agent」的慢性配置
  状态且由平台 QGA 轮询自己触发（24h 最多 48 条），不剔会让 PVE 宿主机巡检
  「异常日志」常年 warning（2026-09-20 实测 20 行全被占满）；修 agent
  问题另走路径（装 qemu-guest-agent / virtio-win），不要往日志项里加回计数。

## RBAC 现状（2026-09-12 收敛后，防重蹈覆辙）

- 权限目录 **8 键 / 5 组**（`app/services/permissions.py` 是唯一真源，前端按 `PERMISSION_GROUPS`
  分组渲染）：`device:view` `device:manage` `device:remote` `pve:view` `pve:manage`
  `automation:manage` `user:manage` `settings:manage`。
- 旧键由迁移 `0034_role_pve_scope_and_perms` 就地改写；`PERMISSION_MIGRATION` 同时兜底解析
  未保存过的历史角色行。映射：`script|inspection|alert:manage`→`automation:manage`、
  `agent:use`→`device:remote`、`docker:control`→`device:manage`+`pve:manage`。
- **`settings:manage` 不再兼任 PVE 管理后门**（迁移给持有者补授 `pve:manage`）。
  新增 PVE 端点一律用 `require_pve_permission` / `require_pve_guest_permission`，
  不要再写 `or settings:manage`。
- 资源授权**只有一个开关** `device_scope`，同时管两张表：`role_device_access`（设备）与
  `role_pve_guest_access`（guest 身份 = `connection_id`+`guest_type`+`vmid`，与
  `business_pve_guests` 同构）。曾短暂引入过 `roles.pve_scope`，已由迁移 `0035` 删除——
  **不要再加回第二个范围字段**，产品口径是"指定设备时一并勾选虚拟机"。
  默认 `all`；`selected` 时未勾任何虚拟机 = 该角色看不到虚拟化资源。
  **凡是枚举 guest 的接口都要过 `get_user_pve_guest_keys`**
  （overview / candidates / containers / dashboard / alerts targets），否则会漏显未授权虚机。
  合成负数 target_id 不含 guest_type，用 `user_can_access_pve_vmid`。
  **连接级接口已收敛（2026-09-20 越权实测后批次 1，勿回退）**：清单对
  view 用户只返有授权虚机的连接且 host/port/token_id 脱敏；overview 走
  `require_pve_connection_permission`（manage 全放行，view 需该连接上有
  ≥1 台授权虚机）；nodes/nextid/storage/isos 收紧为 pve:manage（建 VM
  前置数据，前端本就只在 canManageConn 下调用）。test_pve_connection_rbac.py
  钉死。
  **批次 2（2026-09-20，勿回退）**：PveError→502 对非 pve:manage 用户脱敏
  （`_wrap` 传 user/db，view 可达路径 overview/guest_detail/history/rrd-latest
  都传，manage 用户保留原文案）；`dashboard/overview` 的 room_count 按
  授权设备过滤、user_count 仅 user:manage 可见(其它角色 0)；
  `/rooms/tree` 与 `/dashboard/room-summary` 跳过无授权设备的机房。
  test_dashboard_room_rbac.py 钉死。
  **批次 3（2026-09-20）**：密码策略 = ≥8位 + 3类字符 + 弱口令黑名单
  （`validators._PASSWORD_BLACKLIST`，精确匹配，Pass-1234/Admin@123 这类
  “合规但烂大街”的口令被拒）；detect-os 越权 403 已钉
  （test_pve_connection_rbac）。**审计报告两条与现状不符的结论**：
  “仅校验长度/12345678 被接受”与 “detect-os 越权返回 422” ——均不可在
  当前代码复现（可能测的旧版本；422 的真实来源是请求体缺 ip_address
  的参数校验，与权限无关），再遇到报告时先按测试复现再定性。
  **批次 4（2026-09-20，勿回退）**：`FastAPI(redirect_slashes=False)`
  （尾斜杠 404，消灭 307 Location 的 Host 头注入面，勿改回）；登录限流
  30/15min → **10/minute**（未认证 key 退化为 IP，压喷洒/枚举；别再放宽
  回“本地开发友好”）；/api/health 匿名只回 {"status":"ok"}，database
  字段仅登录后可见。**枚举终结（2026-09-21 同日拍板）**：不存在的用户名也
  做失败计数（`auth._shadow_locks` 影子锁定，阈值/时长/文案与真实账户
  完全同构，锁过期计数不归零，key=strip().lower() 对齐 MySQL CI+PAD SPACE）
  ——真实账户与假用户名在 401/429 及文案上零差异；仅进程内存（多实例
  部署时才需挪 redis）。HSTS/SSH EOL 不属应用代码（HTTPS 终结层/系统运维）。
  test_security_hardening.py 钉死。
- 角色编辑器的候选接口 `/api/roles/pve-guest-candidates` **故意不叠加调用者自身 ACL**，
  否则无法完成授权；业务侧 `/api/businesses/pve-guest-candidates` 才要过滤。
- alembic `version_num` 上限 32 字符，revision id 别起太长。

## 已删除的设备信息字段（勿加回，2026-09-17 用户拍板）

### 已删除的业务监控死端点（勿加回，2026-09-17 用户拍板）

「接口库跨业务复用」的 UI 从未落地，配套端点前端零调用，已整体删除：
`GET /api/service-interfaces`（接口列表）、
`POST /api/businesses/{id}/interfaces/batch` 与
`POST /api/businesses/{id}/interfaces/{interface_id}`（关联已有接口）、
`POST /api/businesses/{id}/servers/{device_id}`（单个关联设备，批量版在用）。
连带删除 schema `InterfaceBrief`、路由助手 `_interface_brief`、前端
`interfaceAPI.list/create/remove`、`businessAPI.linkInterfaces/linkInterface/linkServer`、
类型 `ServiceInterface`。接口生命周期从属业务：创建只走「新建并关联」
（`POST /api/businesses/{id}/interfaces`），编辑/删除走
`PUT/DELETE /api/service-interfaces/{id}`（返回已简化为 message，前端不消费返回体）。
**要重做「接口库复用」请按当时产品方案另起端点，不要恢复这批名字**。

`devices` 表的 `purpose`（用途）/`owner`（负责人）/`web_url`（管理地址）/`mac_address`（MAC）
四列已随迁移 `0047_drop_device_info_columns` 删除，模型/schema/路由/前端类型全链路同步清理。
这些字段从未在前端表单暴露、无任何消费方。需要设备备注类信息时先过产品口径，
**不要顺手恢复这四个字段名**（`validate_mac_address` 验证器也一并删除）。

## 已删除的 3D 场景遗留字段（勿加回，2026-09-17 用户拍板）

`rooms.floor_plan`（3D 平面图 JSON）与 `racks.position_x/y/z`+`rotation`（3D 坐标/朝向）
已随迁移 `0048_drop_3d_legacy_columns` 删除。2D 机房视图全链路零消费。**重做 3D
场景时按当时方案另起字段名，不要复用这批名字**（schema/表单/类型已无此字段）。
注意 `racks.capacity_u` **保留**：它有活语义（cabinet=24/shelf=null，reorder 回写）。

## 已删除的每日快照表与 monitor 统计端点（勿加回，2026-09-17 用户拍板）

`daily_stats_snapshots` 表（monitor 循环每 30s upsert、全前后端零读者）已随迁移
`0049_drop_daily_stats_snapshots` 删除，连带删除 `models/daily_stats.py`、
`monitor._write_daily_snapshot` 与 `/api/monitor/stats` 端点（前端同样零调用，
首页统计走 `/dashboard/overview` 直查 devices 表）。**不要再加回来**——需要
「历史每日在线率」类功能时按当时产品方案重新设计，不要恢复无消费方的快照表。

## 巡检解析必须与 locale 无关（2026-09-18 修，勿回退）

procps 只翻译 free/top/uptime 的**行标签与提示语**，表头与缩写不译：中文机器上
`free -m` 的行标签是「内存：/交换：」（全角冒号，表头行仍是英文），`top` 的
CPU 行冒号变全角「%Cpu(s)：」，`uptime` 是「平均负载：」。旧解析只认英文关键字
（`Mem:` / `%Cpu(s):` / `load average:`）→ 中文机器上 cpu/memory/load 三项
全部解析失败，巡检误报「错误」（用户实测 free 输出正常但内存项直接报错）。
修法双管齐下，**两者都要保留**：① `inspection_commands.LINUX_COMMANDS` 里被关键字
解析的命令前置 `LC_ALL=C`（cpu/memory/load 三项，表头/位置解析的 df/ps 不需要）；
② `inspection_parser` 兼容本地化行（memory 按首个 ≥4 数值列的数据行定位——表头
非数字、Swap 行只有 3 列；cpu/load 正则认全角冒号/中文标签），防环境变量被受限
shell 剩掉。**新增被关键字解析的命令时同样要前置 LC_ALL=C**。指标采集不受影响
（metrics_collector 直接读 /proc/meminfo，与 locale 无关）；agent_commands 的
`free -m` 原样喂给 LLM 读，不需要改。

## 已彻底移除的 LXC 支持（勿加回，2026-09-17 用户拍板）

用户确认「不可能使用到 LXC，以后都不会创建和使用」（真实 PVE 上 21 台 qemu、
0 台 lxc），已全链路移除：
- **创建**：`PveClient.create_lxc`/`list_lxc` 方法删除，`create_guest` 只走 qemu，
  `GuestCreateRequest` 删 ostemplate/password/rootfs_storage；前端创建弹窗删 LXC
  单选与模板字段。
- **操作**：`_validate_gtype`（pve.py/pve_console.py）只认 qemu，lxc 一律 400
  「平台仅支持 qemu 虚拟机(LXC 已移除)」；电源动作表只剩 QEMU_POWER_ACTIONS；
  `parse_guest_volumes` 删 gtype 参数与 bind kind（LXC rootfs/mpN 解析已无入口）。
- **观测**：`pve_guest_status._normalize_guest` 对非 qemu 返回 None（告警/业务/
  容器采集/候选列表等所有下游不感知 lxc）；overview/rrd-latest 列表过滤非 qemu
  行；前端各面板的类型标签固定 VM/虚拟机/QEMU。
- **保留的契约**：`guest_type` 数据列与 `(connection_id, guest_type, vmid)` 三元组
  约束**不动**（schema 固化、RBAC/绑定/业务关联共用，历史上零 lxc 行）；
  `guest_agent_summary`/`fsinfo_summary` 的「非 qemu 不调 QGA」防御分支保留；
  `schemas/business` 的 guest_type pattern 收缩为 `^qemu$`。
- **不要**为新 LXC 需求恢复入口；若将来真出现（他人在 PVE 界面建了 lxc），
  平台表现为「不可见 + API 400」，这是预期状态。

## 两个反复踩的判定坑（已修，勿回退）

- **操作后单机重采必须全量（含 docker stats），不要再引入"跳过 stats 的轻量重采"**
  （2026-09-18 用户实测回退）：持久化是整批 delete+insert，light 探测跳过 stats
  会把**整台主机**所有容器的 CPU/内存快照写成 NULL（不只被操作的那个容器），
  指标全灭直到下个 60s 全量周期；实测 `docker stats --no-stream` ≈2s，
  10s 只是超时上限，跳过它毫无收益。`test_linux_probe_cmd_light_removed_regression_guard` 钉住。

- **OS 判定统一用排除法**：设备侧 `os_system` 含 `windows` → RDP，其余一律 SSH；
  PVE 虚拟机侧（2026-09-14 起）同口径：**Windows 必须被 QGA/ostype/端口探测显式
  确证，确证不了的一律按 Linux 对待**。`_binding_view` 兜底返回 `linux` 并带
  `os_assumed=True` 标记，前端绑定窗口对 assumed/unknown 自动跑免凭据探测
  （打开时 + 保存前带凭据各一次）纠正藏着的 Windows；`binding.os_system` 只存
  `windows`/`linux` 或 NULL（**不要再把字面量 `"unknown"` 写库**），metrics /
  automation / console / files 各消费方对 NULL 一律走 Linux(SSH) 路径，
  不再报「操作系统未识别」。
  绝不能要求字符串里出现 `linux`：真实数据是 `Ubuntu 3ubuntu0.17`、
  `Debian 7+deb13u4` 这类发行版名，不含 `linux` → 两个远程按钮都不渲染
  （曾表现为“云服务器无法 SSH/RDP”）。前端统一用 `utils/osType.ts`。
- **合成负数 target_id 是跨模块契约**：`-(connection_id * 1_000_000 + vmid)`，
  containers / automation / alerts / files 共用，定义在
  `containers_collector.pve_target_id` / `decode_pve_target_id`。新增能寻址虚机的
  端点请沿用它，不要另造一套。该 ID **不携带 guest_type**，ACL 判定用
  `user_can_access_pve_vmid`。
- 文件传输权限分域：设备 = `device:remote` + 设备 ACL；虚机 = `pve:manage` +
  虚拟机 ACL（与其 SSH 控制台同口径）。
- 前端 FileManager 会把未填字段发成**空串而非 null**；`files._resolve_creds`
  已统一把空串归一为 None，否则会覆盖目标上已存凭据导致认证失败。

## RDP 上传卡死 / 画面冻结（2026-09-11 已修，勿回退）

- **`_build_instruction` 长度前缀必须是「字符数」不是「UTF-8 字节数」**。
  Guacamole 协议规范：元素长度 = Unicode 字符数。guacd(C) 和浏览器
  guacamole-common-js(`String.substring`,UTF-16 code unit,BMP 中文字符数一致）
  都按字符数解析。若用字节数，含中文/多字节时前缀与实际不符 → 浏览器切错偏移 →
  **后续所有指令错位解析失败**：画面冻结在上次完整帧、能看到桌面但完全动不了，
  连接和上行鼠标事件却正常（后端日志显示 browser->RDP 一直在收）。
  配套地，`_parse_instruction` 也必须按**字符数**扫 UTF-8 字符边界（数非续字节
  `& 0xC0 != 0x80`），不能按 `data[start:start+length]` 直接按字节切——否则会切到
  多字节字符中间导致 decode 崩溃。
- **`sendEnd` 绝不能在有 blob ack 未回时调用**（`guacUpload.ts`）。
  guacamole-common-js 的 `endStream` 会「发 end + 立即 `delete output_streams[index]`」。
  数据一读完就 sendEnd，此时最后一个窗口的 blob ack 还在路上，到达时流已被删 →
  `onack` 不再触发 → `acked` 永远到不了 `sent` → **卡死在 flushing（实测丢整整一个
  窗口的 ack)**。正确顺序：数据读完后先等 `acked >= sent`，再 sendEnd。这个 bug 在
  滑动窗口（window>1）下必现，window=1 时碰巧不触发。
- 诊断手段：后端 `read_from_guacd`/`write_to_guacd` 计数上下行指令数，前端
  tunnel 层包 `oninstruction` 统计 parser 收到的 ack 数，对比即可定位丢包环节。
  本次根因：后端 61013 进 / 61015 出全对，前端 parser 也收全，是库的路由把尾 ack 丢了。

## 虚拟机巡检/脚本/告警(2026-09-12 收敛，勿回退)

- **PVE 虚拟机在自动化运维中可跑巡检/批量脚本/Agent/电源**。后端 `_execute_job`
  对 `target_type='pve_guest'` 统一经 `_pve_runtime_device` 转成 `AgentTarget`
  （明文凭据/IP/OS，运行时还会用 QGA 复核最新 IP），电源走 PVE API，其余连进
  guest 内部(SSH/WinRM)。前端 `AutomationPanel` 用公共组件
  `components/common/DeviceSelector.vue`(机房分组 + 虚拟化分组卡片，复刻
  角色管理选择器）。
- **AI 归因同样覆盖虚拟机**(2026-09-15)：指标告警事件 `device_id=None`、宿主
  身份在 `resource_id`(合成负数)，`remediation.check_analysis` 闸门对这种事件
  放行，`remediation._analysis_target` 是统一解析入口(物理设备→Device；虚机→
  `_pve_runtime_device`→AgentTarget)。不要再写 `device_id is None` 就拒绝。
  **`agent._execute_run` 必须用 start_diagnosis 传进的内存目标，不能按
  `device.id` 回查 Device 表**：AgentTarget 无 ORM 身份，回查必然报
  「设备不存在」(真环境曾整条告警挂「AI 归因分析失败：设备不存在」)。
- **巡检留档**:`inspection_records.device_id` 允许 NULL(迁移 0036)。虚拟机巡检
  记录 device_id=NULL、device_name/device_ip/target_type 照常冗余写入;PDF 报告
  读冗余列不 join devices，故 NULL 不影响。权限：列表/详情对 device_id IS NULL 的
  记录改用 `user_can_access_pve` 判定。
- **`InspectionRecord`/`DeviceResult` 的 device_id 均为可空**(AgentTarget.id=None)。
- **PDF 统计陷阱(已修)**:`ReportHost.has_problem` 原只看 item 级 problems，主机级
  失败(record 缺失/status=failed)items 为空会被漏算 → 横幅误报「全部正常」。已改
  为 status=failed 即算异常，且明细表对无 item 的失败主机单列一行。
- `connect_device` 对非 ORM 目标(AgentTarget)跳过 TOFU host key 回写(无持久 id)。

## 自动化运维执行模型收敛(2026-09-17，勿回退)

- **旧定时任务系统已整体删除**(迁移 0046 已 drop `scheduled_tasks` 表):
  `routers/scheduled_tasks.py`/`models/scheduled_task.py`/scheduler 的
  `_execute_task`/`_finalize_task`/`recover_interrupted_scheduled_tasks`、
  前端 ScriptPanel「计划任务」tab、`scheduledTaskAPI`、`ScheduledTask` 类型
  全不存在了。定时计划**唯一入口** = `/api/automation/schedules`(创建 POST/
  编辑 PUT `/{id}`/暂停恢复 PATCH `/{id}/status`/删除),目标支持设备与
  PVE 虚机(负数 target_id,授权按正负分流)。**不要再加回脚本专用定时表**
  ——统一计划走 create_job_record,有 steps 审计/虚机支持/巡检报告。
- **计划状态语义**:`paused` = 用户主动暂停(随时恢复;scheduler 到期扫描
  只认 `active`;**恢复时过期的 next_run_at 重算到未来,防补跑暂停期间的
  积压周期**)与 `disabled` = 授权失效系统停用(需排查后重建)严格分开,
  UI 分色。勿混用两个语义。
- **脚本/电源执行内核在 `services/script_exec.py`**(`run_script_on_device`/
  `run_power_on_device`,签名是显式 username/password/ssh_key 关键字参数):
  automation/scheduler/scripts router 都 import 它。**不要再往 router 里写
  执行逻辑再让 service 反向 import**(曾长期层次倒置)。
- **巡检 SSH 复用是独立实现** `services/ssh_session.ReusedSshSession`,与
  agent._SshSession 等价但**故意不合并**——agent.py 是告警归因共用文件,
  巡检侧独立实现是为了不动告警链路;要合并先确认归因路径零改动。
  (CentOS 7 实测:复用后单台 6 项 core 巡检 32.6s→5.9s。)
- **任务目标并发**:`AUTOMATION_MAX_CONCURRENT_TARGETS`(默认 4，
  1=串行)控制 `_execute_job` 目标并发;agent 任务天然单目标;每台结果
  口径/失败隔离/展示顺序与串行一致(测试钉住)。
- **运行数据保留期**:`AGENT_RUN/INSPECTION/AUTOMATION_JOB_RETENTION_DAYS`
  (默认 0=关闭)挂在每小时维护循环;agent_runs 清理**必须跳过仍被
  `alert_events.agent_run_id` 引用的行**(该列无 FK，悬空=归因详情 404)。
- **测试写法坑(必踩)**：TestClient 的 HTTP 提交对持有开启事务的测试会话
  **不可见**(MySQL REPEATABLE READ 快照)——「先 API 创建再 db.query 断言」
  的测试必须在 HTTP 调用后 `db.rollback()` 刷新快照，否则查到 None。
  另两个同类环境坑(2026-09-17 钉)：①开发库与**并行的容器评估循环共享**——
  容器会持续创建真实 open 告警事件(如虚机离线)，任何跑
  `_evaluate_resource_rules` 的用例会按全库语义顺手 submit 它们;
  `_evaluate_resource_rules` 相关断言必须限定自己规则的 rule_id(见
  test_alert_remediation 的 `_my_rule_event_ids`)。②conftest 回收器对
  alert_events 只删「本会话新建规则名下的事件 + rule_id IS NULL 孤儿」,
  真实告警行(rule_id ≤ alert_rules baseline)一律不动——按 id 快照无差别
  删除会误删容器创建的真实告警(实测发生过)。
- **业务监控口径(2026-09-17,勿回退)**：业务告警(`business_status`)评估
  已含 PVE 虚机，口径与业务面板同源：`pve_guest_status.guest_link_state`/
  `judged_guest_health` 是唯一真源(PVE 不可达=不计入分母)；评估在**独立
  循环** `run_business_alert_loop`(不再挂指标采集周期，否则
  METRICS_ENABLED=false 的纯接口监控部署会静默失去业务告警)。接口探测
  有**周期轮消抖**(连续 2 次失败才落 down,同 monitor 语义)与
  **创建/编辑后的立即探测**(`trigger_immediate_probe`,fire-and-forget,
  经 `_loop` 投递,探测器未运行时静默跳过);业务「添加服务器」选择器与
  告警规则同款 DeviceSelector(机房分组+虚机卡片),虚拟机候选经
  `loadGuests` prop 注入(业务只需 pve 权限,不必拿 automation 权限),
  设备候选走 deviceAPI.listByRoom(不按凭据过滤,业务探活不需要凭据;
  曾专设 /api/businesses/server-candidates 端点,选择器改用公共组件后
  已删,勿加回)。
- **明确不做的**：webhook 查询缓存(新增钩子不生效=「开关开了没发」同类
  事故，已否决)；容器告警评估全表收窄(当前 23 行/3ms 无感，容器规模
  >500 行再议，且属于告警中心改动需单独排期)。

## RDP / 终端四个已修陷阱（勿回退）

- **`enable_drive` 必须为 True**：`pve_rdp_ws` 曾写死 `enable_drive=False`，guacd 就
  不下发 `filesystem` 指令 → 前端 `isFilesystemReady` 永为假 → 上传按钮被
  `isGuac && !fsReady` 禁用（点了没反应且无报错）。tests/test_pve_rdp_console.py 钉住。
- **缩放必须等 `Display.onresize`**：`sendSize` 异步生效，`sendScale` 同步调用时读到
  的是旧 framebuffer 尺寸，缩放比会停在错误值上（表现为侧栏收起/展开不对称）。
  guacamole-common-js 的 **Client 没有 `onsize`**，正确钩子是 `client.getDisplay().onresize`。
- **粘贴监听要在 document 捕获**：原来 `paste`/`keydown` 只绑在 display 元素上，
  设备远程页有侧栏/工具栏等可聚焦元素，焦点一离开画面 Ctrl+V 就静默失效
  （而虚机独立窗口没其它可聚焦元素 → 看起来“只有虚机能粘贴”）。
  两处都要跳过 INPUT/TEXTAREA/contenteditable，否则会覆盖远程剪贴板。
- **手动「剪贴板」弹窗已删除**（连同 `pasteTextToRemote` / `triggerRemotePaste`）：
  自动双向同步已足够，两套入口反而让人误以为必须点按钮。
- `Terminal.vue` 的 `enableFiles` 用 `??` 兼底：调用方传 **`false`** 会覆盖默认值。
  曾因 TerminalView 对普通设备显式传 `false` 而把设备侧的文件按钮全关掉；
  不需要时请传 `undefined`。

- **RDP 文件传输只走 GuacamoleFS，不要再加回 SFTP 探测**：`/api/devices/{id}/files/sftp-probe`
  端点与前端探测分支已整体删除。原逻辑是“RDP 先探 OpenSSH，有则 SFTP、无则回落共享盘
  并弹警告横幅”，而实际部署里 Windows 目标都不开 OpenSSH（实测 SRM 与 PVE Windows 虚机
  均 `sftp:false`），提示恒成立，只是白白多一次最长 15s 探测。SSH 仍走 SFTP（没有 guacd，
  没有共享盘可用）。共享盘只能看到映射进桌面的目录，不是远程整机磁盘——要取回其它
  位置的文件得先在 Windows 里复制进共享盘，这一点已在界面用中性提示说明。
- **容器管理页的内嵌容器终端不开 enable-files**（2026-09-17 用户定调，勿加回）：
  Terminal 的“文件”按钮传的是**宿主/虚机**的文件系统（走 /api/files/* SFTP，
  与 docker exec 无关），挂在容器终端上语义误导；要传宿主文件直接去机房管理/
  虚拟化管理的 SSH 入口。设备与 PVE 两个分支都保持纯 docker exec 终端。

## 文件传输吞吐（慢的三个真实原因，已修）

- **GuacamoleFS 上传受协议限制**：guacd 单条指令上限 8192 字节，base64 后每块
  只能装 6048 字节。guacamole-common-js 的 `BlobWriter` 还是**严格一发一收**
  （每块等一个 ack）且每块单独走一次 `FileReader` → 100MB 要一万七千多个
  “异步读 + 一个 RTT”。已改为滑动窗口（`utils/guacBlob.ts` + `uploadFile`）：
  一次读 ~254KB、整块 btoa 后按 8064 字符切分、**16 条在途按 ack 补发**。
  切分等价的前提：6048 是 3 的整数倍，整块 base64 不会跳块产生 padding
  （已有单测钉住）。**不要随意调大 BLOB 长度**，超 8192 会被 guacd 拒收。
- **RDP 上行每条指令被解析 3 次**：`write_to_guacd` 为了鼠标日志/钳位/计数把
  同一条指令 parse 了三遍并重新编码，全部压在事件循环上。已改为
  `_needs_rdp_rewrite()` 字面量嗅探（base64 字符集不含 `.` 和 `,`，无法伪造
  opcode），blob/ack/key/end/sync 直接透传。
- **SFTP 上传阻塞事件循环**：`upload_file` 是 `async def`，却在里面同步跑 SSH
  握手（超时可达 10–30s）和 paramiko 写入——同一循环上还跑着 RDP 桥接，
  会连带拖慢共享盘 ack。已改为：先异步落本地暂存文件（大小上限照常生效）
  → `asyncio.to_thread` 里做传输，并开 `remote.set_pipelined(True)`；下载侧
  加 `remote.prefetch()`。

- **进度条要分两阶段，不能只给一个百分比**：`sending`（数据仍在传，百分比可信）
  与 `flushing`（字节已全部交出，等远端落盘，无法观测 → 用不确定动画 + 文案）。
  共享盘路径的百分比按**已被 ack 的字节**算，不按已发出的；SFTP 路径把 axios 的
  「浏览器→后端」进度压在 99% 以内，请求体发完即切 flushing。
  后端上传已改成**边收边写**（每块 `asyncio.to_thread(remote.write, ...)`），
  不再先落本地暂存文件——暂存会把传输切成两段串行过程，正是「一上传就 99%
  然后卡很久」的成因。

- **上传看门狗必须分两个阶段**：`stallTimeoutMs`（传输途中无 ack，默认 5 分钟）与
  `flushTimeoutMs`（已 `sendEnd`、等远端关流落盘，默认 30 分钟）。guacd 会先收下
  数据并逐条 ack（所以进度很快到 99%），而真正推给 Windows 共享盘发生在关流时，
  期间**没有任何 ack**。曾经用同一个 60s 看门狗覆盖这段静默期，大文件上传
  在落盘阶段被误报“上传超时”。状态机已抽到 `utils/guacUpload.ts`（可用假 stream
  单测，14 例）；在途窗口 32 条，吞吐量 ≈ window × 6048B / RTT，**调窗口就是调速度**。
- **进度回调必须节流**（≥1% 变化或 ≥120ms）：每条 blob 一个 ack，100MB 就是
  一万七千多次回调，每次都会驱动 Vue 响应式与进度条重渲染，全都挤在处理 ack
  的同一个 JS 线程上，反过来拖慢上传本身。
- **小文件慢的是握手开销，不是吞吐**：SFTP 实测局域网 50MB ≈ 1.2s（~42MB/s），
  但每个请求都重走一次完整 SSH 握手：实测 `connect=164ms`、`open_sftp=64ms`、
  `listdir=63ms`，所以 list/upload 恒定 ~0.26s。文件管理器一次“开弹窗 + 上传
  4KB + 刷新列表”就是三次握手 ≈ 0.8s。已用 `services/ssh_pool.py` 复用 transport
  （只新开 SFTP channel，因为 SFTPClient 非线程安全）：list 0.28s→0.070s、4KB
  上传 0.30s→0.097s。长传输中必须 `touch()` 续租，否则会被并发请求触发的
  淘汰关掉正在用的连接。
- **RDP 的 WebSocket 地址分模式（2026-09-18 修，勿回退）**：guacamole-common-js
  需要 `Sec-WebSocket-Protocol` 做子协议协商。**DEV（Vite）必须直连后端**——dev
  代理会吃掉该头；端口解析 `utils/wsBase.ts`：`VITE_WS_PORT` →
  `VITE_BACKEND_PORT` → **8004**（本地 uvicorn）。**PROD（Docker/nginx）必须走
  同源**（`window.location.host`）：nginx 的 /ws 反代实测完整透传子协议（101
  回显 guacamole，guacd 指令正常下发）；而直连后端端口依赖宿主发布端口——
  compose 默认 backend 只绑 127.0.0.1，浏览器从其他机器/用非回环地址访问时
  ws://<LAN IP>:8002 TCP 层不可达 → **RDP 一进会话就 disconnect，SSH 却正常**
  （SSH 走同源代理不受影响，勿被这对现象误导去查 guacd/凭据）。compose 构建注入
  的 `VITE_WS_PORT` 在 PROD 下不参与解析（它默认跟随 BACKEND_PORT=8002，按它拼
  URL 就是直连死路）。曾经硬编码回落到 8002 导致本地开发“SSH 正常、RDP 异常”，
  与本条是同一函数的两个镜像坑。
- docker / docker-compose 相关配置不要改（用户明确要求）。

## 告警中心待办（已分析未实施，勿丢）

- **`evaluate_container_alerts` 两集合收窄**（2026-09-16 分析定案，当前规模
  无收益而暂线）：现状是每 60s 全表 JOIN 加载 device_containers（含 running
  无事件的行，实测 23 容器 21 行白搬）。正确做法是单条 SQL 收窄到
  `state != 'running'` ∪ `存在 pending/open container_status 事件`
  （`or_` + `EXISTS`，两集合并集，绝不只收窄非 running）。**三大陷阱**：
  ① 只收窄到非 running 会让恢复路径漏掉——异常容器回 running 那轮必须进
  resources 才能触发 resolved，否则告警永远 open；② pending 事件的容器必须
  始终保留覆盖，否则被 `close_orphaned_alert_events` 当孤儿误关成假 resolved；
  ③ resources 覆盖面是聚合分组/notified_created 收窄的不变式前提，收窄后
  要重新审计。实施前置：先钉住非 running 触发、running 收敛、pending 覆盖
  三条测试（+guest 容器路径）。必须在告警侧改动批次里单独做，不与其它
  模块混批。

## 门禁命令（交付前必跑）

- backend:
  `.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check .`
  再加
  `set -a && . ../.env && set +a && .venv/bin/python -m pytest tests -q -m "not e2e"`
  （测试直连 3307 的 `dcn` 开发库，conftest 按 id 快照精确清理测试行；也可用 TEST_DATABASE_URL 覆盖）
- **测试不得依赖开发库的「空状态」**：3307 开发库里有真实在用的飞书 webhook
  (订阅 alert.created) 与已保存的 LLM Key。统计通知条数的用例只计数自己创建的
  hook(按 `webhook.id` 过滤)；依赖「Agent 未配置/无已保存密钥」的闸门用例必须
  显式 patch `get_agent_config`，否则库一改配置用例就红。
  **禁止全表清理式 delete**：`test_device_events` 曾在 teardown 里
  `query(Webhook).delete()`，每跑一次门禁就把用户真实 webhook 清掉
  (2026-09-15 真踩过：修完 bug 跑全量后飞书卡片停发)。清理只删本运行创建的
  行(测试前快照自增 max id，只删更大的)。
- frontend:
  `npm run lint && npm run format:check && npx vue-tsc --noEmit -p tsconfig.app.json && npx vitest run && npm run build`

## 门禁"flaky"判定纪律(2026-09-17 观察记录,防误判)

当天 LXC 移除过程中把两次全量失败误判为 flaky(「后台通知线程跨用例泄漏」),
后续排查证据全部推翻该理论:
- `test_alert_remediation.env` fixture setup 以 `db.flush(); db.commit()` 收尾,
  **不存在半开事务窗口**;
- make_event 上加的诊断钩子(打印事务现场)在后续 25+ 轮全量中**零触发**;
- 失败时段与「用 python 脚本批量改测试文件+改断言」的中途态完全重合,
  "单跑通过"的对照是在改完后跑的——对比不公平,失败很可能就是**真实失败**。
教训:**改代码中途跑的全量失败,先怀疑半成品状态,别急着定性 flaky**;
判定 flaky 前必须:(1)代码稳定态复现 2 次以上,(2)看过完整堆栈(不是 summary 行)。
诊断钩子保留在 make_event 里,FK 失败会自动打印事务现场——真 flaky 再次出现时
一次即可定位。勿在无复现的情况下盲改 fixture/线程池。
