# DCN — 数据中心网络管理平台

数据中心网络可视化与运维管理平台，提供机房机柜 2D 可视化、SSH/RDP 远程终端与 SFTP 文件管理、RBAC 权限控制、自动巡检、大屏监控等核心功能。

## 技术架构

```mermaid
graph TB
    subgraph Frontend["前端 (Vue 3)"]
        A[Vite 6 + TypeScript + Pinia + Element Plus]
        B[xterm.js<br/>SSH 终端]
        C[Guacamole JS<br/>RDP 客户端]
        D[ECharts<br/>大屏可视化]
    end

    subgraph Backend["后端 (FastAPI)"]
        E[SQLAlchemy ORM + PyMySQL]
        F[JWT 双令牌认证]
        G[Paramiko<br/>SSH/SFTP]
        H[Guacamole<br/>RDP 代理]
        I[Middleware<br/>安全头 · 追踪 · 限流]
    end

    subgraph Storage["存储"]
        J[(MySQL 8<br/>业务数据)]
        K[(Redis 7<br/>限流/票据/锁)]
    end

    subgraph External["外部"]
        L[guacd<br/>RDP 代理]
        M[PVE API<br/>虚拟化]
        N[LLM API<br/>Agent 诊断]
    end

    Frontend -->|HTTP REST + WebSocket| Backend
    Backend --> J
    Backend --> K
    Backend --> L
    Backend --> M
    Backend --> N
```

### 后端

| 组件 | 说明 |
|------|------|
| **FastAPI** | Web 框架，REST API + WebSocket |
| **SQLAlchemy** | ORM，MySQL 数据库访问 |
| **PyMySQL** | MySQL 驱动 |
| **Alembic** | 数据库迁移（**唯一**的 schema 变更路径） |
| **PyJWT** | JWT 双令牌认证（access 24h + refresh 7d） |
| **Paramiko** | SSH / SFTP 连接管理 |
| **guacd** | Apache Guacamole RDP 代理（Docker） |
| **cryptography** | Fernet 对称加密，设备凭据安全存储 |

### 前端

| 组件 | 说明 |
|------|------|
| **Vue 3** | Composition API |
| **Vite 6** | 构建工具 |
| **TypeScript** | 类型安全 |
| **Pinia** | 状态管理 |
| **Element Plus** | UI 组件库 |
| **xterm.js** | SSH 终端模拟 |
| **Guacamole JS** | RDP 客户端 |
| **ECharts** | 大屏数据可视化 |

## 数据库

项目仅支持 MySQL 8.0。应用启动先由 Alembic 管理迁移，再由 `init_db.py` 幂等初始化内置管理员。当前运行 schema 共 33 张表：

> **Alembic 是唯一的 schema 变更路径。** 新增/修改列一律走 `backend/migrations/versions/`，不要再写一次性的 `ALTER TABLE` 脚本：`init_db.py` 与测试 fixture 用的 `Base.metadata.create_all` 只会创建缺失的**表**，永远不会给已存在的表补**列**。`backend/tests/test_schema_parity.py` 会把模型元数据与真实库结构逐表逐列比对，迁移链一旦欠账就直接失败。

### 核心资源

| 表 | 说明 |
|----|------|
| `rooms` | 机房（名称、位置、描述） |
| `racks` | 机柜/货架（类型、容量 U；cabinet 固定 24U、shelf 灵活高度） |
| `devices` | 设备（服务器/云服务器/台式主机;含 SSH/RDP/WinRM 端口和设备私有加密凭据） |
| `pve_connections` / `pve_guest_bindings` | PVE 平台连接与虚拟机运维绑定 |
| `businesses` / `business_servers` / `business_interfaces` / `business_pve_guests` | 业务监控及服务器、接口、PVE 虚拟机关联 |

### 用户与权限

| 表 | 说明 |
|----|------|
| `users` | 用户（归属角色） |
| `roles` | RBAC 角色（8 项权限 + 单一 all/selected 设备范围，同时管设备与虚拟机） |
| `role_device_access` | 角色→设备细粒度授权 |
| `role_pve_guest_access` | 角色→虚拟机细粒度授权（连接+类型+vmid） |

### 安全

| 表 | 说明 |
|----|------|
| `token_blacklist` | Token 黑名单（主动登出） |

### 运维自动化

| 表 | 说明 |
|----|------|
| `inspection_records` | 巡检记录（按计划/手动触发；虚拟机记录 device_id 为 NULL，凭冗余列定位） |
| `inspection_item_results` | 单设备巡检结果 |
| `device_metric_samples` | 服务器指标时序（CPU/内存/磁盘/负载/网络,默认保留 7 天） |
| `maintenance_windows` | 告警静默窗口（维护期抑制通知，结束后推汇总卡） |
| `agent_runs` | Agent 诊断记录（问题、执行步骤、报告全量留痕） |
| `automation_jobs` / `automation_job_targets` / `automation_job_steps` | 统一自动化任务与执行步骤 |
| `automation_schedules` | 统一自动化计划 |
| `device_docker_status` / `device_containers` / `container_actions` | 普通服务器及 PVE 虚拟机容器状态和操作记录 |
| `alert_rules` / `alert_events` | 告警规则与事件 |
| `service_interfaces` / `interface_probes` | 业务接口定义与探测结果 |
| `webhooks` | 告警 Webhook 配置 |

### 系统

| 表 | 说明 |
|----|------|
| `settings` | 键值系统配置 |

### 时间与时区

- 数据库连接固定 `SET time_zone='+00:00'`，所有 `DATETIME` 存的都是 **UTC 墙钟时间**。
- 模型的时间列统一用 `app.database.UTCDateTime`：写入时剥掉 tzinfo（带偏移的先换算成 UTC），读出时补回 `timezone.utc`。因此接口返回的时间字符串一定带显式偏移（如 `2026-09-08T09:02:15Z`），前端 `new Date()` 能直接换算成浏览器本地时区。
- 前端展示统一走 `frontend/src/utils/datetime.ts`；**不要直接截取 ISO 字符串**，那等于把 UTC 墙钟当本地时间显示（国内会慢 8 小时）。表单里选的本地时间提交前用 `toUtcIso()` 转成 UTC。
- 告警通知（飞书卡片、多维表格 `alert_time`）是服务端渲染好的文本，按 `DISPLAY_TIMEZONE`（默认 `Asia/Shanghai`）转成运维所在时区。

## 权限体系

RBAC 模型，8 项权限按 5 个分组呈现；资源授权只有一个「设备范围」（`all` / `selected`），选 `selected` 时在同一个选择器里既勾机房设备、也勾 PVE 虚拟机：

```mermaid
graph LR
    subgraph 分组["权限分组"]
        A[资源查看]
        B[资源操作]
        C[远程与诊断]
        D[运维编排]
        E[系统管理]
    end

    A --> A1["device:view<br/>查看设备与机房"]
    A --> A2["pve:view<br/>查看 PVE 平台和虚拟机"]
    B --> B1["device:manage<br/>创建/编辑/删除设备与机房"]
    B --> B2["pve:manage<br/>管理 PVE 连接、虚拟机、控制台"]
    C --> C1["device:remote<br/>SSH/RDP/SFTP 远程连接 + Agent 只读诊断"]
    D --> D1["automation:manage<br/>脚本、巡检、告警规则与事件"]
    E --> E1["user:manage<br/>用户、角色管理"]
    E --> E2["settings:manage<br/>系统设置管理"]
```

历史键由迁移 `0034` 就地改写：`script/inspection/alert:manage` → `automation:manage`，`agent:use` → `device:remote`，`docker:control` → `device:manage` + `pve:manage`；`settings:manage` 不再兼任 PVE 管理后门，持有它的角色会被补授 `pve:manage`。

虚拟机授权落在 `role_pve_guest_access`（`connection_id + guest_type + vmid`，与 `business_pve_guests` 同构），因此「指定设备」可以精确到单台 VM。两张白名单表都由 `device_scope` 一个开关控制：`all` 时都不生效，`selected` 时未勾选的虚拟机即不可见。

首次启动（`init_db.py`）自动创建内置「管理员」角色（`is_admin=True`，拥有全部权限）和默认管理员账户。

## 功能特性

### 机房可视化

- 2D 机房视图：机柜/货架布局管理，设备按 U 位精确定位并支持拖放调位，状态颜色实时标识
- 大屏监控视图（概览统计、在线率仪表、设备类型分布、机房摘要）

### 远程终端与文件管理

- **SSH**：xterm.js + Paramiko，多会话，命令历史（仅 Linux/网络设备;Windows 不装 SSH）
- **RDP**：Guacamole 协议，通过 guacd 代理（Windows 远程桌面）
- **文件管理**：目录浏览、上传、下载。传输后端由连接类型唯一决定，**不做探测**：SSH 走 REST/SFTP（复用 SSH 连接、TOFU 主机密钥固定，可浏览任意路径）；RDP 一律走 GuacamoleFS 共享盘（guacd 随会话下发的虚拟盘，需 `enable_drive=True`）。普通设备与 PVE 虚拟机共用同一套端点——虚机用合成负数 `target_id` 寻址，因此虚拟机控制台同样可以传文件
- **RDP 复制粘贴**：本机 Ctrl+V 自动推送到远程剪贴板并合成 Ctrl+V，远程剪贴板变化反向写回本机（无需任何手动「剪贴板」弹窗，该入口已移除）
- WebSocket + 一次性 ticket 认证

### 自动巡检

- 定时巡检计划（可视化周期选择器，无需手写 cron；高级模式下仍可手填任意 5 段 cron）
- 连接设备 → 执行命令 → 解析结果 → 生成报告
- 支持多设备并发巡检；多台结果卡片默认全部收起，靠头部的 ✓/⚠/✕/? 计数定位异常机器
- 通道约定：Linux/网络设备走 SSH,Windows 走 WinRM(PowerShell)
- **失败项不会被当成数据解析**：命令退出码非 0 时直接记为 error,绝不把 stderr 喂给解析器（否则 `_last_percent` 会从 `connect timeout=25` 里捞出 25.0 当成 CPU 使用率,六项全挂的机器看上去「4 项正常」）
- 巡检结果对比与历史趋势

#### 服务异常项的两套语义

| 平台 | item_type | 数据来源 | 含义 |
|------|-----------|----------|------|
| Linux | `failed_services` | `systemctl --failed` | 真正失败的 unit |
| Windows | `services` | 系统日志里 Service Control Manager 的失败事件(7000/7001/7009/7011/7023/7024/7031/7034),按服务名去重、限最近 24 小时 | 真正启动失败/意外终止的服务 |

两者共用阈值 `THRESHOLDS["failed_services"]`（1 个 warning、3 个 critical）。

> Windows 侧**曾经**枚举「`StartType=Automatic` 且没在运行」的服务,那是在数噪音：
> `Get-Service` 分不出「自动(延迟启动)」（.NET 的 `ServiceStartMode` 没有这一档）,
> `sppsvc`、`wuauserv` 这类干完活就自己退出的服务永远在列；`RemoteRegistry`（开着才是安全风险）、
> `spice-agent`（只在用 SPICE 控制台时有意义）本来就该停着。一台健康虚拟机轻松凑齐 3 个,
> 直接被阈值判成 **critical**。现在改查 SCM 失败事件,服务名取自事件的 `ReplacementStrings`
> 而非已本地化的 `Message`,因此在中文 Windows 上同样有效。

### 自动化执行(批量脚本 / 巡检 / Agent 诊断)

- 批量脚本:多设备并发下发(Linux 走 SSH,Windows 走 WinRM PowerShell)
- **Agent 诊断(只读)**:LLM 驱动的服务器排障——用户描述问题,Agent 自主选择只读诊断项取证(CPU/内存/磁盘/IO/进程/服务/日志/端口等),输出结构化报告与处置建议
  - **硬边界**:LLM 只能"点名"预置只读诊断项(`agent_commands.py` 注册表,key 白名单校验),永远不接触 shell 自由输入;注册表命令在加载期强制通过只读校验(不允许重定向/写操作动词/命令替换)
  - Windows「服务异常」诊断项与健康巡检**共用同一条命令**(`inspection_commands.WINDOWS_SCM_FAILURE_COMMAND`):两个面必须给出同一份事实,否则 Agent 报告与巡检结论会互相矛盾。注册进 Agent 时还会再过一次 `assert_readonly`,等于多一道安全网
  - 每次诊断的设备、问题、执行步骤、报告全量落 `agent_runs`;权限并入 `device:remote`（原独立键 `agent:use` 已下线）
  - LLM 走 OpenAI 兼容接口(可配置内网网关/自建模型),默认关闭

### 设备监控

> **纳管范围**：`server` / `cloud_server` / `host` 三种类型全部纳入监控与自动化，判定集合的唯一真源是
> `backend/app/models/device.py` 的 `OPS_TARGET_TYPES`（前端对应 `frontend/src/utils/deviceLabels.ts` 的 `OPS_DEVICE_TYPES`）。
> 云服务器与物理机走同一套远程通道（Linux = SSH，Windows = WinRM/RDP），**不要**在采集、告警、业务关联里再就地写死
> `("server", "host")`——那会让新类型静默失去全部运维能力。`tests/test_validation.py` 会扫描源码拦截这种回退。

- 管理端口 TCP 探测设备在线状态(Windows 探 WinRM/RDP,Linux/网络设备探 SSH)
- 探测带「哨兵端口」守卫:任意高端口都应答握手时判为假在线,避免关机设备被防火墙/NAT 误报为 online。**云服务器豁免该守卫**——云上 SLB/NAT/安全组本来就会应答任意端口,否则健康的云主机会被永久标成 offline(见 `services/monitor.py` 的 `_host_guard_applies`)
- **服务器指标监控**：周期采集 CPU/内存/磁盘/负载/网络速率(Linux 走 SSH 读 `/proc`,Windows 走 WinRM 查 CIM),列表页实时进度条 + 详情页历史曲线(1h/6h/24h/7d)
- **首页曲线概览**：机房管理纳管的全部设备(服务器/云服务器/主机) CPU、内存历史曲线同图对比(1h/6h/24h/7d),数据一次请求返回
- 接口状态采集（up/down、速率;Windows 经 WinRM 查 Get-NetAdapter）
- 电源状态检测（单电源/双电源/冗余）
- 操作系统自动识别(Linux 读 `/etc/os-release`,Windows 经 WinRM 查 Caption 精确到 "Windows Server 2019 Standard")
- 在线用户心跳追踪

### 安全

- JWT 双令牌（access + refresh），主动登出黑名单
- httpOnly Cookie 承载浏览器凭据
- 安全响应头（CSP、Permissions-Policy、X-Frame-Options 等；HTTPS 入口应额外配置 HSTS）
- 速率限制（可配置；多副本部署应将 `RATE_LIMIT_STORAGE` 指向共享 Redis，单副本默认 memory 后端）；登录接口独立 10 次/分钟 IP 级限流
- 账户锁定：5 次失败锁定 30 分钟；**对不存在的用户名同样计数**（影子锁定，文案与真实账户一致，消除用户名枚举信号）
- 密码复杂度校验（最小长度、字符类型要求）+ 常见弱口令黑名单
- 尾斜杠不重定向（关闭 `redirect_slashes`，阻断 Host 头注入到重定向 Location 的面）
- `/api/health` 匿名仅返回存活状态，DB/迁移状态需登录后查看
- 请求追踪 ID（X-Request-ID）

## 项目结构

```
DCN/
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI 入口 + 生命周期
│   │   ├── config.py                # 配置（环境变量 / .env 加载）
│   │   ├── database.py              # SQLAlchemy 引擎与会话
│   │   ├── exceptions.py            # 统一异常定义
│   │   ├── validators.py            # 输入校验
│   │   ├── utils.py                 # 工具函数
│   │   ├── models/                  # ORM 模型（32 张表）
│   │   ├── routers/                 # API 路由（22 个模块）
│   │   │   ├── auth.py              # 认证（登录/登出/刷新/me/在线用户）
│   │   │   ├── rooms.py             # 机房管理
│   │   │   ├── racks.py             # 机柜管理
│   │   │   ├── devices.py           # 设备管理
│   │   │   ├── files.py             # SFTP 文件传输（列目录/上传/下载）
│   │   │   ├── terminal.py          # WebSocket 终端（SSH/RDP）
│   │   │   ├── monitor.py           # 设备监控
│   │   │   ├── dashboard.py         # 仪表盘统计
│   │   │   ├── users.py             # 用户管理
│   │   │   ├── roles.py             # 角色管理
│   │   │   ├── scripts.py           # 脚本管理
│   │   │   ├── inspection.py        # 巡检
│   │   │   ├── pve.py / pve_console.py
│   │   │   ├── containers.py / businesses.py / metrics.py / search.py
│   │   │   └── automation.py / alerts.py / webhooks.py / agent.py
│   │   ├── schemas/                 # Pydantic 请求/响应模型
│   │   ├── services/                # 业务逻辑
│   │   │   ├── auth.py              # JWT、密码验证
│   │   │   ├── crypto.py            # AES 加解密
│   │   │   ├── permissions.py       # RBAC 权限检查
│   │   │   ├── terminal.py / ssh.py # SSH 连接管理 / 客户端封装
│   │   │   ├── guacamole.py         # RDP 代理
│   │   │   ├── monitor.py / interface_prober.py / power.py / os_detect.py
│   │   │   ├── scheduler.py / cron.py  # 定时调度 / cron 解析
│   │   │   ├── inspection*.py       # 巡检引擎 / 命令集 / 结果解析
│   │   │   └── ...
│   │   └── middleware/              # 错误处理 / 安全头 / 追踪 / 限流
│   ├── migrations/                  # Alembic 迁移（**唯一**的 schema 变更路径）
│   ├── tests/                       # pytest 测试
│   ├── init_db.py                   # 首次初始化（建表 + 管理员账户）
│   ├── migrate_rack_24u.py          # 可选的历史数据归一化（非 schema 迁移）
│   ├── backfill_os_system.py        # 可选的一次性回填 os_system（云服务器历史上被跳过 OS 探测）
│   ├── requirements.txt             # 运行时依赖（锁定确切版本）
│   ├── requirements-dev.txt         # 开发/CI 依赖（ruff、pytest 等，同样锁版本）
│   ├── ruff.toml                    # lint + format 规则（CI 两条流水线共用）
│   └── pytest.ini                   # pytest 标记（e2e 需另行起栈）
├── frontend/
│   ├── vitest.config.ts             # 单元测试配置（jsdom，固定 TZ 保证可复现）
│   └── src/
│       ├── App.vue / main.ts
│       ├── api/index.ts             # Axios API 封装
│       ├── router/index.ts          # 路由配置
│       ├── stores/                  # Pinia（auth / room / device）
│       ├── views/                   # Login / Dashboard / BigScreen / Terminal
│       ├── components/
│       │   ├── scene/               # Scene2D / ChassisRenderer / ScenePlaceholder
│       │   ├── terminal/            # Terminal / FileManager / TerminalPlaceholder
│       │   ├── panel/               # 内联面板（机房/设备/PVE/容器/业务/巡检/自动化等）
│       │   └── bigscreen/           # 大屏组件（概览/在线率/类型分布/机房摘要）
│       ├── composables/             # useApi / useTerminal / useGuacamole / ...
│       ├── types/                   # TypeScript 类型
│       └── utils/                   # jwt / sanitize / 标签映射（+ __tests__/ 单元测试）
├── docker/                          # Dockerfile.backend / Dockerfile.frontend / nginx.conf / docker-compose.yml
├── .github/workflows/               # CI（lint、test、build）与发布
└── .env.example                     # 环境变量模板
```

## API 总览

### 认证 `/api/auth`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 登录（凭证仅写入 httpOnly Cookie，响应体不携带 token） |
| POST | `/api/auth/refresh` | 刷新 access token |
| POST | `/api/auth/logout` | 登出（token 加入黑名单） |
| GET | `/api/auth/me` | 当前用户信息与权限 |
| GET | `/api/auth/online-users` | 在线用户列表 |

### 机房 `/api/rooms` · 机柜 `/api/rooms/{room_id}/racks` · 设备 `/api/racks/{rack_id}/devices`

CRUD 操作，支持嵌套路由。设备状态统一为 `online` / `offline`；远程凭据随设备创建或更新，不存在全局凭据目录。

### 文件 `/api/devices/{device_id}/files`

SFTP 目录浏览、文件上传（≤200MB）、下载。

`device_id` 按正负号分流两类目标，权限口径各自独立：

| `device_id` | 目标 | 所需权限 | 资源范围 |
|---|---|---|---|
| `>= 0` | `devices` 行 | `device:remote` | 设备 ACL |
| `< 0` | PVE 虚拟机（`pve_guest_bindings`） | `pve:manage` | 虚拟机 ACL |

负数 ID 是 `-(connection_id * 1000000 + vmid)`，与 containers / automation / alerts 共用同一套编码（`containers_collector.pve_target_id`）。

### 监控 `/api/monitor`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/monitor/statuses` | 所有设备状态列表 |
| POST | `/api/monitor/scan` | 手动触发状态扫描 |

### 仪表盘 `/api/dashboard`

概览统计、设备类型分布、机房摘要。

### 服务器指标 `/api/metrics`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/metrics/devices` | 全部服务器最新指标(RBAC 过滤,含机房/机柜) |
| GET | `/api/metrics/devices/{id}` | 单机最新指标 + 分区明细 |
| GET | `/api/metrics/devices/{id}/history?range=1h/6h/24h/7d` | 历史曲线(自动降采样) |
| GET | `/api/metrics/history?range=1h/6h/24h/7d` | 全部服务器 CPU / 内存曲线,一次请求返回(首页概览) |

### PVE 虚拟化 `/api/pve`

PVE 连接、节点、存储、虚拟机、快照、电源和控制台管理（仅支持 QEMU 虚机，LXC 已移除）。QEMU Guest Agent 可用时优先读取 guest 信息；未配置 QGA 时，使用虚拟机绑定的 SSH/WinRM 凭据按普通服务器方式采集。PVE 虚拟机内存在 Docker 容器时，也会进入容器管理页面。

### 容器管理 `/api/containers`

聚合普通服务器和 PVE 虚拟机中的 Docker 容器，提供列表、详情、日志和 `start` / `stop` / `restart` 控制；没有 Docker 容器的 PVE 虚拟机不会显示。

### 业务监控 `/api/businesses` · `/api/service-interfaces`

维护业务、关联服务器和手工配置的 HTTP 接口，通过服务器在线状态与接口探测结果聚合业务健康度。
业务详情里的"服务器"是统一列表：机房设备与 PVE 虚拟机/容器一起展示、一起统计
（`server_total` / `server_online`），「添加服务器」弹窗按 `机房设备` 与各 `PVE 平台`
分组选择，界面不再区分两类资源。

虚拟机不是 `devices` 记录，其身份是 `连接 + qemu + vmid`，因此走独立关联表；候选列表取自
`GET /api/businesses/pve-guest-candidates`（内存快照，后台循环刷新），关联与取消关联分别是
`POST /api/businesses/{id}/guests/batch` 与 `DELETE /api/businesses/{id}/guests/{link_id}`，
响应中虚拟机以 `kind="pve"` 的服务器条目返回（`link_id` 用于移除）。
PVE 平台不可达时虚拟机显示"状态未知"，且不计入业务健康度分母。

### 告警与自动化 `/api/alerts` · `/api/automation` · `/api/webhooks`

统一管理告警规则/事件、手动或计划自动化任务，以及告警 Webhook 通知。

监控对象树（`GET /api/alerts/targets`）不列出 PVE **模板机**（`/cluster/resources` 里 `template=1` 的克隆源）：模板不运行也没有指标，纳进规则只会产生“永远离线”的假告警，还可能被自愈流程当成关机虚机去启动。同一个判定 `pve.is_guest_template` 也用在规则目标校验、`host_status` 评估和自愈解析上，历史规则里残留的模板目标会被标记为 `skipped` 并说明原因。

告警事件自带自愈能力（`backend/app/services/remediation.py`）：

- **离线自动拉起**：`host_status` 告警的目标若是 PVE 虚拟机则下发 `start`，`container_status` 走 `docker start` 并写入容器操作审计；物理机无法远程上电时标记 `skipped`。
- **进度话术**：处置进度实时拼进 `alert_events.message`（基础描述 + 当前动作 + 归因结论），告警中心与 Webhook 看到的是同一条随时间推进的消息，例如「主机 X 当前已离线或无法连接；已识别为 PVE 虚拟机 [IT-PVE] QEMU 101 web-01，正在下发启动指令…(第 1/2 次)」。
- **进度推送**：状态跃迁（自动处置中 → 等待恢复确认 → 处置成功/失败）以及 AI 归因出结论时，额外推送 `alert.remediation` Webhook；该推送不覆盖告警自身的通知状态，未订阅的部署自动跳过。通知被扣住期间不再推这条，避免与随后放行、已带结论的 `alert.created` 重复。
- **AI 归因**：`cpu_pct` / `mem_pct` / `disk_max_pct` 持续超阈值后调用 Agent 做只读诊断，报告写回事件；列表接口只返回一句话摘要（取报告「结论」小节正文），全文走 `GET /api/alerts/events/{id}`。
- **归因随首条通知一起发**：指标类告警会先把 `alert.created` 扣住，等归因落到终态再放行，于是**同一条**消息的 `message` 与 `analysis.text` 里就带着结论（飞书卡片的「AI 归因」块直接有内容），不必等第二条推送。为此扣住期间不再等 `ALERT_ANALYSIS_SUSTAIN_SECONDS`，防抖交给规则自己的 `sustain_seconds`。放行点覆盖所有出口（闸门拒绝/启动失败/完成/失败/事件已恢复/超时/进程重启），并由 `sweep_expired_notification_holds` 看门狗兜底——告警最多迟到 `ALERT_ANALYSIS_HOLD_TIMEOUT_SECONDS`（默认 180s），绝不会被吞掉；把 `ALERT_ANALYSIS_HOLD_NOTIFICATION` 设为 `false` 即恢复旧的两条消息行为。
- **闸门**：最大尝试次数、冷却时间、同一事件单飞三重约束，避免反复启动。`POST /api/alerts/events/{id}/remediate` 与 `POST /api/alerts/events/{id}/analyze` 支持人工重跑，不满足条件时返回 409 并说明原因。
- **失去评估来源即收尾**：评估器只恢复“本轮还覆盖得到”的事件，所以规则停用/删除、对象被移出规则范围、对象整行被删掉之后，遗留的 `open` 事件靠自己永远不会恢复，告警中心一直显示未恢复、概览被死数据顶着、订阅方也等不到 `alert.resolved`。收尾统一走 `close_orphaned_alert_events()`（`backend/app/services/alerts.py`）：`pending`（持续时长未满、从未对外触发）直接删除不留历史，`open` 置为 `resolved`、消息按原因补一句说明并推 `alert.resolved`（「告警规则已停用/已删除/已不存在，事件自动关闭」「监控对象已移出该告警规则的范围，事件自动关闭」「监控对象已被删除，事件自动关闭」）；扣住等归因的通知一并丢弃（来源都没了，再补一对 created/resolved 只是噪音），未收尾的自动处置状态收敛为 `skipped`。改规则的 `metric` 或目标范围、以及删规则时，接口当场收尾并返回 `closed_event_count` 供前端提示；其余情形（直接改库停用、删设备/删业务/删 PVE 平台/在 PVE 侧销毁虚机）由启动时 + 每个采集周期（`METRICS_INTERVAL`，默认 60s）的清扫收敛。
- **只有数据库能证明的事实才关事件**：“这轮没采到”和“对象暂时不可达”绝不等于“对象没了”——采集失败的设备恰恰是最需要保持告警的。容器类事件要求该服务器最近一次 Docker 探测确实成功（`device_docker_status.available=1`）才认“清单里没有它 = 容器被删了”，因为容器清单每轮整表重写、探测失败时也会写空；PVE 虚机同理，要求 `pve_guest_status` 里该连接 `reachable=True` 且快照不超过 `_PVE_SNAPSHOT_MAX_AGE`（`max(120s, PVE_STATUS_INTERVAL×4)`）才认“清单里没有这个 vmid = 虚机已销毁”，因此在 Proxmox 侧删掉虚机（连接仍保留）也会在一个周期内收敛，而 PVE 不可达、进程刚启动、本实例不是 leader 时一律保持 `open`。
- **维护静默窗口**：`maintenance_windows`（`/api/alerts/maintenance-windows`）指定对象+时段，窗口内这些对象的告警通知在出口被抑制（事件照常创建/评估/归因/处置，告警中心照常可见），窗口结束后持续异常的告警由冷却重发自然浮出，同时推一条汇总卡告知静默期间抑制了多少次通知。
- **删规则不等于删历史**：规则是配置、事件是运维留痕。`alert_events.rule_id` 用 `ON DELETE SET NULL`（迁移 `0031`），删除规则只断开关联，事件连同它的 AI 归因结论全部保留；规则名在事件创建时就写入 `alert_events.rule_name` 快照，所以历史里仍看得清是哪条规则触发的（改名前后的记录各自准确），删除规则时会顺带补全历史空值。事件列表/详情对 `alert_rules` 改用外连接，规则没了照样查得到，接口里 `rule_id` 为 `null` 时以 `rule_name` 兜底；概览的 `active_rules` 只统计仍关联着规则的事件，不再被已删规则的死数据顶着。
- **归因被重启打断 ≠ 归因失败**：`recover_orphaned_analyses()` 按原因分两种收尾——被进程重启掐断的（`agent_runs.error` 为 `INTERRUPTED_RUN_ERROR`、一个诊断步骤都没跑完）退回「未分析」，下一轮评估自动重跑；真跑出失败结论的（LLM 报错/超时/无结论）才保留 `failed`，并把原因写进消息，如「AI 归因未得出结果(诊断超过 300s 未返回结论)」。
- **开关**：见 `.env.example` 的「告警自愈」段落；AI 归因还需在「系统管理 → Agent 配置」中启用 Agent 并配置 LLM。

Webhook 通道按 `provider` 区分（`backend/app/services/alerts.py`）：

| provider | 投递方式 |
|----------|----------|
| `generic` / `wecom` / `dingtalk` | 原样 POST 完整告警 JSON，可带自定义请求头与 `X-Webhook-Secret` |
| `feishu` | 飞书工作流/多维表格 Webhook，只发 `device_name`、`severity`、`alert_content` 等扁平字段 |
| `feishu_bot` | 飞书群自定义机器人，支持加签；只能发到机器人所在的群 |
| `feishu_app` | 飞书企业自建应用，调 `im/v1/messages` **直接私聊指定的人** |

`feishu_app`（`backend/app/services/feishu.py`）不需要再绕多维表格工作流：

1. 在[飞书开放平台](https://open.feishu.cn/app)创建「企业自建应用」，申请 `im:message`（以应用身份发送单聊、群聊消息）权限并开启机器人能力；要在界面里直接读通讯录选人，再加 `contact:contact.base:readonly`（获取通讯录基本信息）。飞书还会按**字段级权限**裁剪返回内容——**必需**：`contact:user.base:readonly`（姓名/头像）、`contact:department.base:readonly`（部门名/人数），缺了界面只能显示 ID；**可选**：`contact:user.email:readonly`（按邮箱搜索、副标题显示邮箱；投递不依赖邮箱，选人写入的是 `open_id`）、`contact:user.employee_id:readonly`（授权范围里单独勾选的成员）。后端用 `name_scope_missing=true` 标记缺名称权限，界面直接提示要补哪个，名称退回显示邮箱前缀或 ID（`feishu._display_name`）。权限改动要创建版本并发布才生效。
2. 让企业管理员在 飞书管理后台（admin.feishu.cn）→ 工作台 → 应用管理 → 该应用 → 应用权限 → 通讯录设置 里设置「通讯录权限范围」：设为**全部员工**可浏览整个组织架构；只勾选**指定部门 / 指定成员**同样可用——飞书此时不允许读根部门（返回 `[40004] no dept authority error`），后端会自动改用 `GET /contact/v3/scopes` 拿到授权范围，再用 `departments/batch`、`users/batch` 批量换成部门名称与成员信息，弹窗只显示被授权的那部分并给出说明（`webhooks._scoped_departments` / `_scoped_users`，响应里的 `scope_limited=true`）。授权范围里单独勾选的成员需要应用具备 `contact:user.employee_id:readonly` 才会返回。
3. 「系统设置 → Webhook 告警」新建通道，Provider 选「飞书自建应用（私聊指定人）」，App ID 与 App Secret 并排填写（Secret 加密存进 `webhooks.secret_enc`）。
4. 点「从通讯录选择」按部门逐层下钻、按姓名/邮箱搜索并多选成员；选中的人写入 `open_id`，姓名存进 `config.receiver_names` 只用于界面回显（通讯录接口：`POST /api/webhooks/feishu/departments`、`POST /api/webhooks/feishu/users`，凭据可取已保存通道或表单现填值，Secret 只走请求体不进日志）。取消勾选一个已在列表里的人，写入时会同步移除。
5. 也可以在「手动添加」框里粘贴接收人：邮箱免通讯录权限，`ou_` 开头的 `open_id`、`on_` 开头的 `union_id` 指定某个人，`oc_` 开头的 `chat_id` 发到指定群，其余按 `user_id` 处理；支持一次粘贴多个（逗号/分号/空白分隔），逐个投递，上限 50。
6. 界面上**不需要选 ID 类型**：`config.receive_id_type` 默认 `auto`，投递时按每个接收人字符串的形态自动识别（`feishu.infer_receive_id_type`）；仍可通过 API 显式指定 `email` / `open_id` / `user_id` / `union_id` / `chat_id` 让整条通道沿用同一类型。
7. 消息**统一为交互式卡片**（标题色随级别变化、恢复变绿，正文分块展示自动处置与 AI 归因）：`config.style` 默认 `card`，界面上不再提供切换，需要纯文本时可通过 API 写成 `text`。卡片上的「当前值」按指标类型渲染（`feishu._value_label`）：性能类是 `92.6%（阈值 90.0%）`，状态类（`host_status` / `container_status` / `business_status`）内部是 `1=异常、0=正常` 的编码，会翻译成「离线 / 在线」「异常 / 正常」；事件里存的是触发时的值，恢复通知一律显示正常态。

`tenant_access_token` 进程内缓存并提前 5 分钟续期，遇到 token 失效自动刷新重试一次。provider 专属参数存放在 `webhooks.config` JSON 列，由 alembic `0030_webhook_provider_config` 添加。开放平台域名固定为国内站 `https://open.feishu.cn`（界面上不再提供该配置项，后端 `feishu.normalize_base` 兜底并做 SSRF 校验）；海外 Lark 部署需要改通道记录的 `url` 字段为 `https://open.larksuite.com`。

### Agent 诊断 `/api/agent`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/agent/status` | Agent 启用/配置状态 |
| GET | `/api/agent/devices` | 可诊断设备列表(server/cloud_server/host) |
| GET | `/api/agent/catalog/{id}` | 设备可用的只读诊断项 |
| POST | `/api/agent/diagnose` | 执行一次只读诊断(LLM tool-calling) |
| GET | `/api/agent/runs` | 诊断历史 |

### 巡检 `/api/inspection`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/inspection/records` | 巡检记录列表 |
| POST | `/api/inspection/run` | 手动执行巡检 |
| GET | `/api/inspection/records/{id}` | 巡检详情（含逐设备结果） |

### 统一自动化计划 `/api/automation/schedules`

定时计划（巡检/批量脚本）的唯一入口：创建/编辑/暂停恢复/删除。目标支持物理设备与 PVE 虚拟机（合成负数 target_id，授权按正负分流）；每次到期执行都落 `automation_jobs`，有完整 steps 审计。`paused`（用户主动暂停，可随时恢复）与 `disabled`（授权失效被系统停用，需排查后重建）严格区分。

### 脚本 `/api/scripts`

批量命令执行与批量关机/重启（Linux 走 SSH，Windows 走 WinRM）。

### 用户与角色 `/api/users` · `/api/roles`

用户 CRUD + 启用/禁用 + 角色归属；角色 CRUD + 权限配置。

### 终端

WebSocket `/ws/terminal/{device_id}?ticket=...` — SSH/RDP 终端会话（一次性 ticket 认证）。

## 快速开始

### 环境要求

- [Docker](https://docs.docker.com/get-docker/) 与 Docker Compose v2
- 或手动安装：Python 3.12+、Node.js 18+（数据库与 RDP 代理仍建议用 Docker）

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，修改所有 change-me 占位符（必填项见下方「配置参考」）
```

### 2. Docker Compose 一键部署（推荐）

```bash
docker compose -f docker/docker-compose.yml --env-file .env up -d
```

首次启动会自动构建前后端镜像（多阶段构建，镜像自包含、不挂源码卷），之后 `up` 会复用缓存。代码变更后 `up --build` 强制重建。

启动 6 个服务：MySQL、Redis、后端、前端（Nginx）、guacd-init、guacd。

访问 `http://localhost:8080`，默认账户 `admin` / `.env` 中配置的 `ADMIN_PASSWORD`。

### 3. 本地开发（前后端分离）

适合二次开发：前后端在本机热重载运行，MySQL 与 guacd 用 Docker 提供。

**① 启动依赖服务（MySQL + Redis + guacd）：**

```bash
docker compose -f docker/docker-compose.yml --env-file .env up -d mysql redis guacd
```

**② 配置 `.env` 指向本机服务**

`.env.example` 里的 `DATABASE_URL` / `GUACD_HOST` / `RATE_LIMIT_STORAGE` 默认指向 Docker 内网服务名（`mysql` / `guacd` / `redis`)。本地开发需改为：

```bash
# .env
DATABASE_URL=mysql+pymysql://root:${MYSQL_ROOT_PASSWORD}@127.0.0.1:3306/dcn?charset=utf8mb4
GUACD_HOST=127.0.0.1
RATE_LIMIT_STORAGE=memory://
WS_TICKET_STORAGE=memory://
```

**③ 后端（Python 虚拟环境，默认开发端口 8004）：**

```bash
cd backend
python -m venv .venv          # 推荐 Python 3.12
# Windows:  .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

# 首次运行/升级：先执行迁移，再创建默认管理员
python -m alembic upgrade head
python init_db.py

uvicorn app.main:app --host 0.0.0.0 --port 8004 --reload
```

> **从旧版升级且库里已有云服务器时**，补跑一次 `os_system` 回填。设备表单早期把「支持 Windows 远程」写死成
> `server`/`host`，云服务器因此跳过了 OS 自动探测，`os_system` 留空；而该字段决定远程通道（Windows 探 WinRM 5985、
> Linux 探 SSH 22，巡检与批量脚本同理），留空会让 Windows 云主机的在线状态、巡检和自动化全部执行失败。
> 脚本只填 `os_system` 为空的行，不覆盖已有值，可安全重跑：
>
> ```bash
> cd backend
> python backfill_os_system.py --dry-run                     # 先探测看结果，不写库
> python backfill_os_system.py --type cloud_server --yes     # 只回填云服务器
> ```
>
> 新建的云服务器在表单里会自动探测，无需再跑。

> 前端 Vite 代理默认连接 `localhost:8004`；可通过 `VITE_BACKEND_PORT` 覆盖。请确保它与本地 uvicorn 端口一致。
>
> `DATABASE_URL` 需指向一个**已存在的 MySQL 账户与库**。全新 Docker 数据卷会按 `.env` 的 `MYSQL_DATABASE`、`MYSQL_USER`、`MYSQL_PASSWORD` 自动创建应用库与 `dcn_app` 用户。若从旧版本升级且复用已有 `mysql-data` 卷，MySQL 不会重新执行用户初始化；首次升级前请用 root 补建应用账户：
>
> ```sql
> CREATE USER IF NOT EXISTS 'dcn_app'@'%' IDENTIFIED BY '<MYSQL_PASSWORD>';
> ALTER USER 'dcn_app'@'%' IDENTIFIED BY '<MYSQL_PASSWORD>';
> GRANT ALL PRIVILEGES ON dcn.* TO 'dcn_app'@'%';
> FLUSH PRIVILEGES;
> ```
>
> 然后确认 `.env` 中 `MYSQL_USER=dcn_app`、`MYSQL_PASSWORD=<同一密码>`，再执行 `docker compose -f docker/docker-compose.yml --env-file .env up -d`。后端入口会运行 `alembic upgrade head`，迁移失败时容器不会带着不完整 schema 启动。
>
> 本机 3306 若被占用，在 `.env` 设 `MYSQL_PORT=3307`（compose 宿主机映射端口）并把 `DATABASE_URL` 端口同步改为 3307。

**④ 前端（端口 5040）：**

```bash
cd frontend
npm install
npm run dev
```

访问 `http://localhost:5040`。

### 4. 运行测试

测试只使用 MySQL，不会读取项目 `.env` 中的业务库。请先准备一个可丢弃的
测试库，然后通过 `TEST_DATABASE_URL` 指定连接串：

```bash
cd backend
TEST_DATABASE_URL='mysql+pymysql://root:<password>@127.0.0.1:3306/dcn_test?charset=utf8mb4' python -m alembic upgrade head
TEST_DATABASE_URL='mysql+pymysql://root:<password>@127.0.0.1:3306/dcn_test?charset=utf8mb4' python -m pytest tests -m 'not e2e'
```

Windows PowerShell 可使用 `$env:TEST_DATABASE_URL='...'` 后再执行上述 Python
命令。测试不会自动创建或修改生产数据库。

## 配置参考

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATABASE_URL` | — | MySQL 连接串（必填） |
| `JWT_SECRET` | 自动生成 | JWT 签名密钥；生产必须显式设置（否则重启后所有 token 失效） |
| `JWT_ALGORITHM` | HS256 | JWT 签名算法 |
| `JWT_EXPIRE_HOURS` | 24 | Access token 有效期 |
| `REFRESH_TOKEN_EXPIRE_DAYS` | 7 | Refresh token 有效期 |
| `ADMIN_USERNAME` | admin | 默认管理员用户名 |
| `ADMIN_PASSWORD` | 自动生成 | 默认管理员密码；未设置时生成随机值并写入 `data/.admin_initial_password` |
| `CREDENTIAL_SECRET_KEY` | — | 凭据加密密钥（必填，不可自动生成，否则历史凭据无法解密） |
| `GUACD_HOST` / `GUACD_PORT` | localhost / 4822 | guacd 地址与端口 |
| `MYSQL_PORT` | 3306 | MySQL 宿主机映射端口（Docker Compose） |
| `MONITOR_INTERVAL` | 30 | 设备监控间隔（秒） |
| `MONITOR_TIMEOUT` | 2 | 管理端口 TCP 探测超时（秒） |
| `MONITOR_CONCURRENCY` | 20 | 监控并发数 |
| `RATE_LIMIT_ENABLED` | true | 启用速率限制 |
| `RATE_LIMIT_STORAGE` | memory:// | 限流计数存储（Docker Compose 部署下默认 redis://redis:6379/0）；多副本须指向共享 Redis |
| `WS_TICKET_STORAGE` | memory:// | WebSocket 一次性 ticket 存储（Docker Compose 部署下默认 redis://redis:6379/0） |
| `BACKGROUND_TASKS_ENABLED` | true | 是否在该实例运行采集与调度；多副本仅一个实例开启 |
| `DISPLAY_TIMEZONE` | Asia/Shanghai | 告警通知（飞书卡片 / 多维表格）里时间的展示时区；数据库统一存 UTC，留空则跟随服务器系统时区 |
| `DCN_AUTO_CREATE` | false | 启动时自动建表（仅开发便利；生产用 Alembic） |
| `CORS_ORIGINS` | — | 允许的跨域来源（不允许 `*`） |
| `COOKIE_SECURE` | false | 生产 HTTPS 下设 true（纯 HTTP 访问必须保持 false，否则浏览器拒收 Cookie） |
| `FRONTEND_PORT` | 8080 | 前端 Nginx 端口（Docker） |
| `BACKEND_PORT` | 8002 | 后端端口（Docker，仅回环监听） |
| `VITE_WS_PORT` | 8080 | RDP WebSocket 端口（**构建期**变量，需重建前端镜像；推荐 = FRONTEND_PORT 走 nginx 同源，详见 .env.example） |
| `COMPOSE_PROJECT_NAME` | dcn | Compose 项目名（容器/卷命名前缀；未设时取 compose 目录名） |

## 许可证

Internal use. All rights reserved.
