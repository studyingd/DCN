# DCN — 数据中心网络管理平台

数据中心网络可视化与运维管理平台，提供机房机柜 2D/3D 可视化、SSH/RDP 远程终端、RBAC 权限控制、会话审计、自动巡检、大屏监控等核心功能。

## 技术架构

```
┌──────────────────────────────────────────────────────────────┐
│                     Frontend (Vue 3)                          │
│   Vite 6 + TypeScript + Pinia + Element Plus                 │
│   xterm.js (SSH)  ·  Guacamole (RDP)  ·  ECharts (大屏)      │
└─────────────────────────┬────────────────────────────────────┘
                          │ HTTP REST + WebSocket
┌─────────────────────────▼────────────────────────────────────┐
│                    Backend (FastAPI)                          │
│   SQLAlchemy ORM + PyMySQL + JWT + Paramiko + Guacamole      │
│   Middleware: 安全头 · 请求追踪 · 速率限制 · 统一错误处理       │
└──────┬──────────────┬────────────────┬───────────────────────┘
       │              │                │
┌──────▼──────┐ ┌─────▼──────┐ ┌──────▼────────┐
│   MySQL 8   │ │ guacd      │ │ RustFS (S3)   │
│  数据存储    │ │ RDP 代理   │ │ 录像 / 审计存储 │
└─────────────┘ └────────────┘ └───────────────┘
```

### 后端

| 组件 | 说明 |
|------|------|
| **FastAPI** | Web 框架，REST API + WebSocket |
| **SQLAlchemy** | ORM，MySQL 数据库访问 |
| **PyMySQL** | MySQL 驱动 |
| **Alembic** | 数据库迁移 |
| **PyJWT** | JWT 双令牌认证（access 24h + refresh 7d） |
| **Paramiko** | SSH 连接管理 |
| **guacd** | Apache Guacamole RDP 代理（Docker） |
| **RustFS / MinIO** | S3 兼容对象存储，会话录像保存 |
| **pycryptodome** | AES-256 对称加密，凭据安全存储 |

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

MySQL 8.0，应用启动自动建表，Alembic 管理迁移。共 17 张表：

### 核心资源

| 表 | 说明 |
|----|------|
| `rooms` | 机房（名称、位置、平面图） |
| `racks` | 机柜/货架（类型、坐标、容量 U） |
| `devices` | 设备（服务器/交换机/路由器/防火墙/主机） |
| `connections` | 设备间连接（以太网/光纤/串口） |
| `credentials` | 远程凭据（AES 加密，支持 SSH 密钥） |

### 用户与权限

| 表 | 说明 |
|----|------|
| `users` | 用户（支持角色+用户组双层归属） |
| `roles` | RBAC 角色（8 项权限 + all/specific 设备范围） |
| `role_device_access` | 角色→设备细粒度授权 |
| `user_groups` | 用户组 |

### 审计与合规

| 表 | 说明 |
|----|------|
| `audit_logs` | 审计日志（命令执行/拦截、会话启停、登录） |
| `session_recordings` | 会话录像元数据（asciicast v2） |
| `token_blacklist` | Token 黑名单（主动登出） |

### 运维自动化

| 表 | 说明 |
|----|------|
| `inspection_records` | 巡检记录（按计划/手动触发） |
| `inspection_item_results` | 单设备巡检结果 |
| `scheduled_tasks` | 定时任务（巡检计划） |
| `daily_stats_snapshots` | 每日统计快照 |

### 系统

| 表 | 说明 |
|----|------|
| `settings` | 键值系统配置 |

## 权限体系

RBAC 模型，8 项权限，角色支持 `all` / `specific` 两种设备访问范围：

| 权限标识 | 说明 |
|----------|------|
| `device:view` | 查看设备与机房 |
| `device:manage` | 创建/编辑/删除设备与机房 |
| `device:remote` | SSH/RDP 远程连接 |
| `script:manage` | 脚本管理 |
| `credential:manage` | 凭据管理 |
| `user:manage` | 用户、角色、用户组管理 |
| `audit:manage` | 审计日志与策略管理 |
| `inspection:manage` | 巡检管理 |

首次启动自动创建 `admin` 角色和默认管理员账户。

## 功能特性

### 机房可视化
- 机柜/货架自由拖放，2D 平面图 + 3D 机箱渲染
- 设备按 U 位精确定位，状态颜色实时标识
- 设备连接拓扑可视化
- 大屏监控视图（设备统计、巡检概览、审计时间线、登录趋势）

### 远程终端
- **SSH**：xterm.js + Paramiko，多会话，命令历史
- **RDP**：Guacamole 协议，通过 guacd 代理
- WebSocket + 一次性 ticket 认证
- 会话自动录制（asciicast v2），支持回放与下载
- 命令拦截与实时审计

### 自动巡检
- 定时巡检计划（cron 表达式调度）
- 连接设备 → 执行命令 → 解析结果 → 生成报告
- 支持多设备并发巡检
- 巡检结果对比与历史趋势

### 审计系统
- 会话级审计分组（按 session_id 聚合）
- 命令执行/拦截记录，登录历史追踪
- 会话录像管理与回放
- 数据保留策略自动清理
- 每日统计快照

### 设备监控
- ICMP 后台轮询检测设备在线状态
- 接口状态采集（up/down、速率）
- 电源状态检测（单电源/双电源/冗余）
- 操作系统自动识别
- 在线用户心跳追踪

### 安全
- JWT 双令牌（access + refresh），主动登出黑名单
- 安全响应头（CSP、HSTS、X-Frame-Options 等）
- 速率限制（可配置，支持 Redis/memory 后端）
- 密码复杂度校验（最小长度、字符类型要求）
- 请求追踪 ID（X-Request-ID）

## 项目结构

```
DCN/
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI 入口 + 生命周期
│   │   ├── config.py                # 配置（DB、JWT、MinIO 等）
│   │   ├── database.py              # SQLAlchemy 引擎与会话
│   │   ├── exceptions.py            # 统一异常定义
│   │   ├── validators.py            # 输入校验
│   │   ├── utils.py                 # 工具函数
│   │   ├── models/                  # ORM 模型（17 张表）
│   │   │   ├── room.py
│   │   │   ├── rack.py
│   │   │   ├── device.py
│   │   │   ├── connection.py
│   │   │   ├── credential.py
│   │   │   ├── user.py
│   │   │   ├── role.py
│   │   │   ├── role_device_access.py
│   │   │   ├── user_group.py
│   │   │   ├── audit_log.py
│   │   │   ├── session_recording.py
│   │   │   ├── token_blacklist.py
│   │   │   ├── inspection.py
│   │   │   ├── inspection_item.py
│   │   │   ├── scheduled_task.py
│   │   │   ├── daily_stats.py
│   │   │   └── setting.py
│   │   ├── routers/                 # API 路由（15 个模块）
│   │   │   ├── auth.py              # 认证（登录/登出/刷新/me）
│   │   │   ├── rooms.py             # 机房管理
│   │   │   ├── racks.py             # 机柜管理
│   │   │   ├── devices.py           # 设备管理
│   │   │   ├── connections.py       # 设备连接
│   │   │   ├── credentials.py       # 凭据管理
│   │   │   ├── monitor.py           # 设备监控
│   │   │   ├── dashboard.py         # 仪表盘统计
│   │   │   ├── audit.py             # 审计与录像
│   │   │   ├── users.py             # 用户管理
│   │   │   ├── roles.py             # 角色管理
│   │   │   ├── terminal.py          # WebSocket 终端
│   │   │   ├── inspection.py        # 巡检
│   │   │   ├── scheduled_tasks.py   # 定时任务
│   │   │   └── scripts.py           # 脚本管理
│   │   ├── schemas/                 # Pydantic 请求/响应模型
│   │   ├── services/                # 业务逻辑
│   │   │   ├── auth.py              # JWT、密码验证
│   │   │   ├── crypto.py            # AES 加解密
│   │   │   ├── permissions.py       # RBAC 权限检查
│   │   │   ├── terminal.py          # SSH 连接管理
│   │   │   ├── ssh.py               # SSH 客户端封装
│   │   │   ├── guacamole.py         # RDP 代理
│   │   │   ├── ws_ticket.py         # WebSocket 一次性凭证
│   │   │   ├── token_blacklist.py   # Token 黑名单
│   │   │   ├── monitor.py           # ICMP 设备监控
│   │   │   ├── interface_status.py  # 接口状态采集
│   │   │   ├── power.py             # 电源状态检测
│   │   │   ├── os_detect.py         # 操作系统识别
│   │   │   ├── online_users.py      # 在线用户心跳
│   │   │   ├── recorder.py          # 终端会话录制
│   │   │   ├── storage.py           # S3 存储
│   │   │   ├── retention.py         # 数据保留策略
│   │   │   ├── settings.py          # 系统配置
│   │   │   ├── inspection.py        # 巡检引擎
│   │   │   ├── inspection_commands.py  # 巡检命令集
│   │   │   ├── inspection_parser.py    # 巡检结果解析
│   │   │   ├── scheduler.py         # 定时任务调度器
│   │   │   └── cron.py              # Cron 表达式解析
│   │   └── middleware/
│   │       ├── error_handler.py     # 统一异常处理
│   │       ├── security_headers.py  # 安全响应头
│   │       ├── trace.py             # 请求追踪 ID
│   │       └── rate_limiter.py      # 速率限制
│   ├── migrations/                  # Alembic 迁移
│   ├── init_db.py                   # 数据库初始化脚本
│   ├── conftest.py                  # Pytest 配置
│   └── test_*.py                    # 测试文件
├── frontend/
│   └── src/
│       ├── App.vue                  # 根组件
│       ├── main.ts                  # 入口
│       ├── api/index.ts             # Axios API 封装
│       ├── router/index.ts          # 路由配置
│       ├── stores/                  # Pinia 状态
│       │   ├── auth.ts              # 认证（JWT、权限）
│       │   ├── rooms.ts             # 机房数据
│       │   └── device.ts            # 设备数据
│       ├── views/
│       │   ├── LoginView.vue        # 登录页
│       │   ├── DashboardView.vue    # 主面板（3 列布局）
│       │   ├── BigScreenView.vue    # 大屏监控
│       │   └── TerminalView.vue     # 终端独立页
│       ├── components/
│       │   ├── scene/
│       │   │   ├── Scene2D.vue      # 2D 可视化画布
│       │   │   ├── ChassisRenderer.vue  # 3D 机箱渲染
│       │   │   └── ScenePlaceholder.vue # 占位组件
│       │   ├── terminal/
│       │   │   ├── Terminal.vue     # SSH/RDP 终端容器
│       │   │   └── TerminalPlaceholder.vue
│       │   ├── panel/               # 内联面板
│       │   │   ├── NavTree.vue      # 左侧导航树
│       │   │   ├── HomePanel.vue    # 系统首页
│       │   │   ├── RoomsPanel.vue   # 机房管理
│       │   │   ├── DeviceDetail.vue # 设备详情
│       │   │   ├── DeviceForm.vue   # 设备表单
│       │   │   ├── RoomForm.vue     # 机房/机柜表单
│       │   │   ├── RackForm.vue     # 机柜表单
│       │   │   ├── CredentialPanel.vue  # 凭据管理
│       │   │   ├── CredentialForm.vue   # 凭据表单
│       │   │   ├── BindDevicesDialog.vue # 设备绑定
│       │   │   ├── AuditPanel.vue   # 审计中心
│       │   │   ├── ReplayModal.vue  # 录像回放
│       │   │   ├── InspectionPanel.vue  # 巡检面板
│       │   │   ├── InspectionResultCard.vue # 巡检结果
│       │   │   ├── ScriptPanel.vue  # 脚本管理
│       │   │   ├── UserManagePanel.vue   # 用户管理
│       │   │   ├── UserManageDialog.vue  # 用户/角色表单
│       │   │   ├── ConnectionForm.vue    # 连接表单
│       │   │   ├── ConnectionList.vue    # 连接列表
│       │   │   ├── ContextMenu.vue       # 右键菜单
│       │   │   └── ScheduledTaskDialog.vue # 定时任务
│       │   └── bigscreen/           # 大屏组件
│       │       ├── BigScreenHeader.vue   # 头部
│       │       ├── OverviewStats.vue     # 概览统计
│       │       ├── OnlineRateGauge.vue   # 在线率仪表
│       │       ├── DeviceTypeChart.vue   # 设备类型分布
│       │       ├── TopologyGraph.vue     # 拓扑图
│       │       ├── RoomSummary.vue       # 机房摘要
│       │       ├── AuditTimeline.vue     # 审计时间线
│       │       ├── LoginTrendChart.vue   # 登录趋势
│       │       └── ScriptStats.vue       # 脚本统计
│       ├── composables/             # 组合式函数
│       │   ├── useApi.ts            # API 调用封装
│       │   ├── useTerminal.ts       # SSH 终端逻辑
│       │   ├── useGuacamole.ts      # RDP 客户端逻辑
│       │   ├── useRdpReplay.ts      # RDP 录像回放
│       │   ├── useAuditLoader.ts    # 审计数据加载
│       │   ├── useDeviceMonitor.ts  # 设备状态轮询
│       │   ├── useBigScreenPoller.ts # 大屏数据轮询
│       │   ├── useInterfaceStatus.ts # 接口状态
│       │   └── useFullscreen.ts     # 全屏切换
│       ├── types/                   # TypeScript 类型
│       │   ├── index.ts
│       │   ├── dashboard.ts
│       │   └── inspection.ts
│       └── utils/                   # 工具函数
│           ├── jwt.ts               # JWT 解析
│           ├── sanitize.ts          # XSS 防护
│           ├── deviceLabels.ts      # 设备标签映射
│           └── inspectionLabels.ts  # 巡检标签映射
├── docker/
│   ├── Dockerfile.backend
│   ├── Dockerfile.frontend
│   ├── nginx.conf
│   └── docker-entrypoint-backend.sh
├── .github/workflows/
│   ├── ci.yml                      # CI（lint、test、build）
│   └── release.yml                 # 发布流程
├── docker-compose.yml              # 生产部署（5 服务）
├── .env.example                    # 环境变量模板
├── .dockerignore
└── pyproject.toml
```

## API 总览

### 认证 `/api/auth`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 登录（返回 access + refresh token） |
| POST | `/api/auth/refresh` | 刷新 access token |
| POST | `/api/auth/logout` | 登出（token 加入黑名单） |
| GET | `/api/auth/me` | 当前用户信息与权限 |
| GET | `/api/auth/online-users` | 在线用户列表 |

### 机房 `/api/rooms` · 机柜 `/api/rooms/{room_id}/racks` · 设备 `/api/racks/{rack_id}/devices`

CRUD 操作，支持嵌套路由。设备额外支持状态管理（online/offline/maintenance）。

### 凭据 `/api/credentials`

CRUD + 解密查看 + 设备绑定/解绑。

### 监控 `/api/monitor`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/monitor/stats` | 设备统计（总数/在线/离线/维护） |
| GET | `/api/monitor/statuses` | 所有设备状态列表 |
| GET | `/api/monitor/interface-status` | 接口状态 |
| GET | `/api/monitor/power-status` | 电源状态 |
| POST | `/api/monitor/scan` | 手动触发状态扫描 |

### 仪表盘 `/api/dashboard`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/dashboard/summary` | 概览统计 |
| GET | `/api/dashboard/device-types` | 设备类型分布 |
| GET | `/api/dashboard/login-trend` | 登录趋势 |
| GET | `/api/dashboard/daily-stats` | 每日统计 |

### 审计 `/api/audit-*` · 录像 `/api/recordings`

会话命令审计、登录历史、录像管理与回放。

### 巡检 `/api/inspection`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/inspection/records` | 巡检记录列表 |
| POST | `/api/inspection/run` | 手动执行巡检 |
| GET | `/api/inspection/records/{id}` | 巡检详情（含逐设备结果） |

### 定时任务 `/api/scheduled-tasks`

CRUD + 手动触发 + Cron 预览。

### 脚本 `/api/scripts`

脚本上传与管理（shell/bat/python 等）。

### 用户与角色 `/api/users` · `/api/roles` · `/api/user-groups`

用户 CRUD + 启用/禁用 + 角色+用户组归属；角色 CRUD + 权限配置。

### 系统配置 `/api/settings`

GET/PUT 键值配置。

### 终端

WebSocket `/ws/terminal/{device_id}?ticket=...` — SSH/RDP 终端会话（一次性 ticket 认证）。

## 快速开始

### 环境要求

- [Docker](https://docs.docker.com/get-docker/) 与 Docker Compose v2
- 或手动安装：Python 3.13+、Node.js 18+、MySQL 8.0+

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，修改所有 change-me 占位符
```

### 2. Docker Compose 一键部署（推荐）

```bash
docker compose up -d
```

启动 5 个服务：MySQL、后端、前端（Nginx）、guacd、RustFS。

访问 `http://localhost:8080`，默认账户 `admin` / `.env` 中配置的密码。

### 3. 本地开发

**数据库：**

```sql
CREATE DATABASE dcn CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

**后端：**

```bash
cd backend
pip install -e ..
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

首次启动自动建表并初始化默认账户。

**RDP 代理：**

```bash
docker compose up -d guacd
```

**前端：**

```bash
cd frontend
npm install
npm run dev
```

访问 `http://localhost:5173`。

## 配置参考

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATABASE_URL` | — | MySQL 连接串（必填） |
| `JWT_SECRET` | — | JWT 签名密钥（必填，`openssl rand -hex 32`） |
| `JWT_ALGORITHM` | HS256 | JWT 签名算法 |
| `JWT_EXPIRE_HOURS` | 24 | Access token 有效期 |
| `REFRESH_TOKEN_EXPIRE_DAYS` | 7 | Refresh token 有效期 |
| `ADMIN_USERNAME` | admin | 默认管理员用户名 |
| `ADMIN_PASSWORD` | — | 默认管理员密码（必填） |
| `CREDENTIAL_SECRET_KEY` | — | 凭据加密密钥（必填，`openssl rand -hex 32`） |
| `GUACD_HOST` | localhost | guacd 地址 |
| `GUACD_PORT` | 4822 | guacd 端口 |
| `MINIO_ENDPOINT` | — | S3 存储地址 |
| `MINIO_ACCESS_KEY` | — | S3 Access Key |
| `MINIO_SECRET_KEY` | — | S3 Secret Key |
| `MONITOR_INTERVAL` | 30 | 设备监控间隔（秒） |
| `MONITOR_TIMEOUT` | 2 | ICMP 超时（秒） |
| `MONITOR_CONCURRENCY` | 20 | 监控并发数 |
| `RATE_LIMIT_ENABLED` | true | 启用速率限制 |
| `RATE_LIMIT_STORAGE` | memory:// | 速率限制存储后端 |
| `CORS_ORIGINS` | — | 允许的跨域来源 |
| `FRONTEND_PORT` | 8080 | 前端 Nginx 端口（Docker） |
| `BACKEND_PORT` | 8002 | 后端端口（Docker） |

## 许可证

Internal use. All rights reserved.
