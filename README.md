# RepoForge

> 🔧 自动化代码工厂 - 基于飞书的 AI 辅助代码修复与开发系统

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-Required-2496ED.svg)](https://www.docker.com/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## 📖 关于 RepoForge

RepoForge 是一个**自动化代码工厂系统**，通过飞书聊天界面接收开发任务，利用 LLM（大语言模型）自动分析代码库、定位问题、生成修复补丁，最终创建 GitHub Pull Request。

### 核心能力
- ✅ **自动化 Issue 修复** - 从聊天需求到代码补丁的全自动流程
- ✅ **跨仓库动态接管** - 支持 `[repo: username/repo]` 标签，实时克隆任意 GitHub 仓库
- ✅ **安全沙盒隔离** - Docker 容器 + Git Worktree 双重隔离，危险命令拦截
- ✅ **多智能体协作** - Planner（规划）→ Coder（编码）→ Reviewer（审查）三段式流水线
- ✅ **自动 PR 创建** - 修复完成后自动推送分支并创建 Pull Request
- ✅ **MCP 协议解耦** - 工具层与 Agent 层完全解耦，便于维护和扩展
- ✅ **生产级监控** - Prometheus 指标 + 结构化日志，可观测性完善
- ✅ **优雅停机** - 信号处理，任务优雅完成，资源自动清理
- ✅ **并发任务处理** - 线程池控制（默认5个并发），支持5-10个任务同时运行

## 🏗️ 架构设计

```
┌─────────────────────────────────────────────────────────────┐
│                      飞书聊天界面 (ChatOps)                  │
│             用户发送: "修复登录bug" 或 "[repo:xxx] 添加功能"   │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                feishu_bot.py (任务调度中心)                   │
│   - 接收消息并解析                                             │
│   - 创建任务 (task_id)                                        │
│   - 动态克隆仓库或创建本地 Worktree                           │
│   - 启动 Docker 沙盒                                          │
│   - 调用 Agent 流水线                                         │
│   - 实时更新飞书卡片状态                                      │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                    Agent 智能体层                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │  Planner     │→ │    Coder     │ →│   Reviewer   │     │
│  │  (架构师)     │  │  (执行者)     │  │  (审查者)     │     │
│  │              │  │              │  │              │     │
│  │ • 代码库探索  │  │ • 读取 plan   │  │ • 审查 diff  │     │
│  │ • AST 地图   │  │ • 精准修改    │  │ • 决策通过/  │     │
│  │ • 生成 plan  │  │ • 测试验证    │  │   拒绝        │     │
│  └──────────────┘  │ • 反思循环(3轮)│  └──────────────┘     │
│                    └───────────────┘                        │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                  MCP Client 接口层                            │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  mcp_client.py                                       │  │
│  │  - 统一工具调用接口                                   │  │
│  │  - 屏蔽底层沙盒实现细节                               │  │
│  └──────────────────────────────────────────────────────┘  │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                  Sandbox 沙盒隔离层                          │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  Docker 容器 (python:3.11-slim)                      │  │
│  │  - 挂载 Worktree 到 /workspace                       │  │
│  │  - user=uid:gid (权限一致)                           │  │
│  │  - 危险命令拦截                                      │  │
│  │  - Git 环境配置                                      │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### 关键技术特性

| 特性 | 实现方式 |
|------|----------|
| **任务隔离** | Git Worktree（每个任务独立目录 + 分支） |
| **执行隔离** | Docker 容器（代码修改在容器内进行） |
| **权限安全** | 容器 user=uid:gid 确保文件权限一致 |
| **命令安全** | CommandInterceptor 黑名单拦截 |
| **状态路由** | task_id 全链路传递 + TaskRegistry 映射 |
| **质量保障** | 3轮反思循环 + Reviewer 审查 |
| **网络容错** | 指数退避重试（OpenAI API、GitHub API、Git push） |
| **架构解耦** | MCP Client 接口层，Agent 与沙盒实现分离 |
| **并发控制** | ThreadPoolExecutor（默认5个并发，可配置5-10） |
| **线程安全** | 全链路锁保护（TaskRegistry、WorktreeManager、MCPClient） |
| **可观测性** | Prometheus 指标 + JSON 结构化日志 |
| **运维保障** | 优雅停机，资源自动清理，活跃任务等待 |

## 🚀 快速启动

### 环境要求

- **Python 3.10+** - 推荐 3.11 或 3.12
- **Docker Desktop** - 需已启动并运行
- **Git** - 命令行可用
- **OpenAI 兼容 API** - DeepSeek、StepFun 或官方 OpenAI
- **飞书应用** - 已创建机器人并获得 App ID/Secret

### 安装步骤

1. **克隆项目**
   ```bash
   git clone <your-repo-url>
   cd RepoForge
   ```

2. **创建虚拟环境**（推荐）
   ```bash
   python -m venv venv
   # Windows
   venv\Scripts\activate
   # Linux/Mac
   source venv/bin/activate
   ```

3. **安装依赖**
   ```bash
   pip install -r requirements.txt
   ```

4. **配置环境变量**
   ```bash
   cp .env.example .env
   ```

   编辑 `.env` 文件，填写必要配置：

   ```env
   # OpenAI 兼容 API 配置
   OPENAI_API_KEY=your_api_key_here
   OPENAI_BASE_URL=https://api.deepseek.com
   MODEL_ID=deepseek-chat

   # 飞书机器人配置（必填）
   FEISHU_APP_ID=cli_xxxxxx
   FEISHU_APP_SECRET=your_secret_here

   # GitHub 配置（跨仓库克隆和 PR 创建需要）
   GITHUB_TOKEN=ghp_xxxxxxxxxxxx
   GITHUB_REPO=your_org/your_repo  # 默认仓库，可被 [repo:xxx] 覆盖

   # 可选配置
   TARGET_REPO_PATH=/path/to/local/repo  # 本地仓库路径，默认项目根目录
   LOG_LEVEL=INFO  # DEBUG/INFO/WARNING/ERROR
   PROMETHEUS_PORT=8000  # Prometheus 指标端口，设为空字符串禁用

   # 🔥 并发控制（新增）
   MAX_CONCURRENT_TASKS=5  # 最大并发任务数，建议5-10（根据机器配置调整）
   ```

5. **启动飞书机器人**
   ```bash
   python -m chat_ops.feishu_bot
   ```

   成功启动后，你会看到：
   ```
   🚀 飞书机器人启动成功！
   ```

6. **在飞书中测试**
   - 向配置的机器人发送消息：
     ```
     修复项目中的登录bug
     ```
   - 或动态接管其他仓库：
     ```
     [repo: username/repo] 请帮我添加用户注册功能
     ```

## 📋 配置详解

### 必填配置

| 环境变量 | 说明 | 示例 |
|---------|------|------|
| `OPENAI_API_KEY` | LLM API 密钥 | `sk-xxxxx` |
| `OPENAI_BASE_URL` | API 地址 | `https://api.deepseek.com` |
| `MODEL_ID` | 模型标识 | `deepseek-chat` |
| `FEISHU_APP_ID` | 飞书应用 ID | `cli_xxxxxx` |
| `FEISHU_APP_SECRET` | 飞书应用密钥 | `xxxxx` |

### 可选配置

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `GITHUB_TOKEN` | - | GitHub Personal Access Token，用于克隆和创建 PR |
| `GITHUB_REPO` | - | 默认目标仓库 `owner/repo` |
| `TARGET_REPO_PATH` | 项目根目录 | 本地代码库路径（用于 worktree 创建） |
| `LOG_LEVEL` | `INFO` | 日志级别：DEBUG/INFO/WARNING/ERROR |
| `PROMETHEUS_PORT` | `8000` | Prometheus 指标暴露端口，设为空字符串禁用 |
| `MAX_CONCURRENT_TASKS` | `5` | 🔥 最大并发任务数，建议5-10（根据机器配置调整） |

### 获取飞书 App ID 和 Secret

1. 打开 [飞书开发者后台](https://open.feishu.cn/)
2. 创建应用 → 选择"机器人"类型
3. 在"基础信息"页获取 App ID 和 App Secret
4. 配置机器人权限：`im:message`、`im:chat` 等
5. 发布应用并安装到目标聊天群

### 生成 GitHub Token

1. 访问 GitHub → Settings → Developer settings → Personal access tokens
2. 生成新 token，勾选权限：
   - `repo` (Full control of private repositories)
   - `public_repo` (Public repositories)
3. 复制 token 到 `.env` 的 `GITHUB_TOKEN`

## 🔧 故障排查

### 问题：机器人启动失败，提示 "FEISHU_APP_ID missing"

**解决方案**：检查 `.env` 文件是否配置了 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`，并确保 `python -m chat_ops.feishu_bot` 在项目根目录运行。

---

### 问题："Docker not available" 或 "Cannot connect to Docker"

**解决方案**：
1. 确认 Docker Desktop 已启动
2. 在终端运行 `docker ps` 验证连接
3. Windows 用户需确保 Docker 使用 WSL2 后端

---

### 问题：任务执行时提示 "Permission denied"（文件权限）

**原因**：Docker 容器内创建的文件在宿主机权限错误（常见于 Linux/macOS）。

**解决方案**：代码已内置修复（`sandbox/container.py:65`），确保容器使用 `user=uid:gid`。如果仍有问题，检查 Docker 版本 >= 7.0.0。

---

### 问题：Git push 失败，提示 "Permission denied (publickey)"

**解决方案**：
1. 确认 `GITHUB_TOKEN` 配置正确且有 `repo` 权限
2. 检查网络代理设置（GitHub 访问）
3. 验证 token 未过期

---

### 问题：OpenAI API 调用超时或失败

**解决方案**：
1. 检查 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY` 是否正确
2. 验证网络连接（某些服务需代理）
3. 查看日志：默认 INFO 级别，可改为 `LOG_LEVEL=DEBUG` 获取详情

---

### 问题：任务卡在 "running" 状态，无进展

**排查步骤**：
1. 检查 Docker 容器是否运行：`docker ps`
2. 查看容器日志：`docker logs <container_id>`
3. 确认 LLM API 是否正常响应
4. 增加日志级别：`.env` 中设置 `LOG_LEVEL=DEBUG`

---

### 问题：Windows 上删除 worktree 失败（权限不足）

**解决方案**：代码已使用 `robust_rmtree` 处理只读文件。如果仍有问题，手动删除 `.feishu_worktrees/` 目录。

---

### 问题：缺少依赖模块（如 `module 'structlog' has no attribute 'get_logger'`）

**解决方案**：安装最新依赖：
```bash
pip install -r requirements.txt
```
确保包含 `structlog>=23.0.0` 和 `prometheus-client>=0.18.0`。

---

### 问题：Prometheus 指标无法访问

**解决方案**：
1. 检查端口 8000 是否被占用：`netstat -an | grep 8000`
2. 通过 `.env` 禁用：`PROMETHEUS_PORT=`（空值）
3. 查看日志确认 metrics server 是否启动

---

### 问题：优雅停机无效或超时

**解决方案**：
- 使用 `kill -TERM <pid>` 或 `Ctrl+C` 发送 SIGINT
- 系统会等待活跃任务最多 30 秒后强制退出
- 查看日志确认收到 "Received shutdown signal"

---

### 问题：并发任务过多导致系统资源耗尽

**原因**：同时运行的任务数超过机器承载能力（内存/CPU/Docker 资源）。

**解决方案**：
1. 调整 `.env` 中的 `MAX_CONCURRENT_TASKS`（建议 4-8）：
   ```env
   MAX_CONCURRENT_TASKS=5  # 默认5个，4GB内存机器建议设为3-4
   ```
2. 查看当前并发任务数：访问 Prometheus 指标 `http://localhost:8000/metrics` 查看 `repoforge_active_tasks`
3. 紧急情况下，重启机器人会自动清理所有沙盒资源

---

### 问题：任务在队列中等待过久

**原因**：所有并发槽位都被占用，新任务在队列中等待。

**排查**：
1. 检查 `repoforge_active_tasks` 指标是否持续高位
2. 查看日志中是否有任务长时间运行（超过 30 分钟）

**解决方案**：
- 临时增加 `MAX_CONCURRENT_TASKS`（需重启）
- 优化任务分配策略（可扩展优先级队列）
- 考虑升级机器配置（更多内存/CPU）

---

## 🧪 运行测试

项目包含单元测试，覆盖核心模块：

```bash
# 安装测试依赖
pip install pytest pytest-mock

# 运行所有测试
pytest tests/ -v

# 运行特定测试
pytest tests/test_interceptor.py -v
pytest tests/test_worktree_manager.py -v
```

### 测试说明

- `test_interceptor.py` - 危险命令拦截、路径验证（23个测试用例）
- `test_worktree_manager.py` - Git worktree 生命周期管理（11个测试用例，需要 git 可用）

## 🛠️ 开发指南

### 项目结构

```
RepoForge/
├── agent/                      # Agent 智能体
│   ├── super_agent.py         # Coder Agent（核心执行引擎）
│   ├── planner_agent.py       # Planner Agent（架构规划）
│   └── reviewer_agent.py      # Reviewer Agent（代码审查）
├── chat_ops/                   # 聊天操作层
│   ├── feishu_bot.py          # 飞书机器人主程序
│   ├── feishu_cards.py        # 飞书卡片构建器
│   └── card_manager.py        # 卡片会话管理器
├── mcp_client.py              # MCP 客户端接口（统一工具调用）
├── mcp_tools/                 # MCP 工具实现（共享库）
│   ├── tools.py
│   └── main.py
├── sandbox/                   # 沙盒隔离层
│   ├── container.py          # Docker 容器管理
│   ├── worktree.py           # Git worktree 管理
│   ├── registry.py           # TaskRegistry（task_id 路由）
│   └── interceptors.py       # 危险命令拦截
├── utils/                     # 工具模块
│   ├── retry.py              # 重试装饰器（网络调用）
│   ├── logging_config.py     # 结构化日志配置（structlog）
│   ├── monitoring.py         # Prometheus 指标收集
│   └── ...
├── config.py                 # 统一配置管理（单例模式）
├── requirements.txt         # Python 依赖
├── .env.example             # 环境变量模板
└── README.md                # 本文档
```

### 添加新的 Agent 类型

1. 在 `agent/` 目录创建新文件，例如 `agent/debugger_agent.py`
2. 定义 `run_debugger_task()` 函数，接收 sandbox 控制器
3. 在 `feishu_bot.py` 的 `execute_swe_task()` 中调用

示例：
```python
# agent/debugger_agent.py
def run_debugger_task(task_prompt: str, sandbox_controller):
    # 实现调试逻辑
    pass

# chat_ops/feishu_bot.py
from agent.debugger_agent import run_debugger_task

# 在合适的位置调用
run_debugger_task("分析错误日志", sandbox)
```

### 自定义重试策略

`utils/retry.py` 提供了灵活的装饰器：

```python
from utils.retry import retry, retry_network

# 自定义重试
@retry(max_attempts=5, base_delay=2, backoff_factor=3)
def my_custom_operation():
    # ...
    pass

# 网络操作（已预定义）
@retry_network(max_attempts=3)
def api_call():
    # ...
    pass
```

### MCP 接口使用

`mcp_client.py` 提供了与底层沙盒交互的统一接口，Agent 层应通过 `MCPClient` 而非直接导入 `sandbox` 模块：

```python
from mcp_client import get_mcp_client

mcp = get_mcp_client()
# 或指定路径
mcp = MCPClient(workdir=Path("/path/to/repo"), worktrees_base=Path("/path/to/worktrees"))

# 调用工具
result = mcp.file_read(task_id, "/workspace/file.py")
mcp.file_write(task_id, "/workspace/output.txt", "content")
output = mcp.bash_execute(task_id, "pytest tests/")
mcp.worktree_create(task_id, "branch_name", base_ref="HEAD")
```

### 🔥 并发控制与线程安全

系统使用 **ThreadPoolExecutor** 实现并发控制，默认限制 5 个任务同时执行（可通过 `MAX_CONCURRENT_TASKS` 调整）。

#### 核心组件线程安全设计

1. **TaskRegistry** (`sandbox/registry.py`)
   - 使用 `threading.Lock` 保护所有字典操作
   - `register()`, `get()`, `unregister()`, `list_tasks()`, `get_worktree_path()` 全部加锁

2. **MCPClient** (`mcp_client.py`)
   - 双重检查锁定（Double-Checked Locking）实现线程安全单例
   - 确保全局只有一个 MCPClient 实例

3. **WorktreeManager** (`sandbox/worktree.py`)
   - 基于 `worktrees_base` 路径的全局 `RLock` 注册表
   - `_load_index()` 和 `_save_index()` 操作共享的 `index.json` 时加锁
   - 使用 `RLock` 支持重入，避免死锁

4. **任务调度** (`chat_ops/feishu_bot.py`)
   - 使用 `ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TASKS)` 替代无限创建线程
   - `process_swe_task_wrapper()` 包装器提供线程级异常处理
   - 优雅停机时调用 `executor.shutdown(wait=True)` 确保资源释放

#### 调整并发数

在 `.env` 文件中配置：

```env
MAX_CONCURRENT_TASKS=8  # 根据机器配置调整（建议5-10）
```

**推荐配置**：
- 4GB 内存：3-4 个并发
- 8GB 内存：6-8 个并发
- 16GB+ 内存：10-12 个并发

#### 监控并发状态

访问 Prometheus 指标：
```
http://localhost:8000/metrics
```

关键指标：
- `repoforge_active_tasks` - 当前活跃任务数
- 任务队列大小（可通过扩展 `TASK_EXECUTOR._work_queue.qsize()` 暴露）

#### 已知限制与改进方向

- **无界队列**：当前 ThreadPoolExecutor 使用无限队列，极端情况下会无限等待。可扩展为有界队列 + 拒绝策略（返回"系统繁忙"）。
- **完全原子性**：WorktreeManager `create()` 的检查-创建操作依赖 git 文件系统锁，极端 race 会失败（已可接受，失败后重试即可）。
- **分布式锁**：当前锁仅限单机。如需多机部署，需替换为 Redis 分布式锁。

## 📊 监控与运维

### 结构化日志

项目使用 **structlog** 输出 JSON 格式的结构化日志，便于日志收集和分析。

**日志级别配置**：在 `.env` 中设置 `LOG_LEVEL=DEBUG|INFO|WARNING|ERROR`

示例 JSON 日志输出：
```json
{
  "timestamp": "2025-04-01T12:34:56.123456",
  "logger": "agent.super_agent",
  "level": "info",
  "event": "=== Iteration 1/20 ===",
  "task_id": 42
}
```

### Prometheus 指标

系统自动暴露 Prometheus 指标，默认在 **端口 8000** (`http://localhost:8000/metrics`)。

**核心指标**：
- `repoforge_tasks_started_total` - 启动的任务总数
- `repoforge_tasks_completed_total{status="success|failure"}` - 完成的任务总数
- `repoforge_task_duration_seconds` - 任务执行时长（直方图）
- `repoforge_active_tasks` - 当前活跃任务数（仪表盘）
- `repoforge_agent_iterations_total{agent="planner|coder|reviewer"}` - Agent 迭代次数
- `repoforge_api_call_seconds{agent}` - API 调用耗时
- `repoforge_container_create_total` / `container_destroy_total` - 容器操作次数
- `repoforge_worktree_create_total` / `worktree_remove_total` - 工作树操作次数

**Grafana 仪表盘**：导入 `monitoring/grafana_dashboard.json` 即可快速搭建监控视图。

### 优雅停机

支持 `SIGINT`（Ctrl+C）和 `SIGTERM` 信号：
- 立即停止接收新消息
- 等待活跃任务完成（最多 30 秒）
- 自动清理所有沙盒容器和工作树
- 安全退出进程

**禁用指标服务器**：在 `.env` 中设置 `PROMETHEUS_PORT=`（空值）

## 📊 生产部署建议

### 日志收集

JSON 结构化日志可直接对接日志聚合系统：
- **ELK Stack**：Filebeat → Logstash → Elasticsearch → Kibana
- **Grafana Loki**：Promtail → Loki → Grafana Explore

建议采集字段：`timestamp`, `level`, `logger`, `event`, `task_id`, `duration`

### 资源清理

- **Worktree 目录**：`.feishu_worktrees/` - 成功任务自动清理，失败或保留现场的任务会保留
- **动态克隆仓库**：`.dynamic_repos/` - 任务完成后自动删除
- **Docker 容器**：每个任务创建后，在 finally 块销毁

**手动清理所有资源**：
```bash
# 停止所有运行中的容器
docker ps -q --filter "name=repoforge-" | xargs -r docker stop

# 删除 worktrees
rm -rf .feishu_worktrees/
rm -rf .dynamic_repos/

# 清理未使用的 Docker 镜像（可选）
docker image prune -f
```

### 性能调优建议

- **API 调用**：已内置重试（3次，指数退避），可根据网络情况调整 `retry.py`
- **并发控制**：通过 `MAX_CONCURRENT_TASKS` 配置线程池大小（默认5个），建议根据机器配置调整：
  - 4GB 内存：建议 3-4 个并发
  - 8GB 内存：建议 6-8 个并发
  - 16GB+ 内存：建议 10-12 个并发
- **任务队列**：当前使用 ThreadPoolExecutor 无界队列，如需限制排队数量可扩展为有界队列 + 拒绝策略
- **监控告警**：在 Grafana 中设置规则，当 `active_tasks` 突增或 `task_duration_seconds` 超过阈值时告警
- **Docker 资源**：确保 Docker Desktop 分配足够内存（建议 >= 4GB）和 CPU（建议 >= 2核）

## 📝 待办事项 (Roadmap)

- [x] **并发控制与线程安全** - 修复 TaskRegistry、MCPClient、WorktreeManager 的线程安全问题，添加 ThreadPoolExecutor 限制（5-10个并发）
- [ ] 实际集成 DeerFlow MCP 服务器（解耦 Agent 调用）
- [ ] 添加数据库持久化（任务状态历史）
- [ ] 支持多种 LLM 提供商（Claude、Gemini）
- [ ] 实现 Webhook 替代轮询（飞书）
- [ ] 添加 Prometheus 指标端点（已完成基础指标，可扩展）
- [ ] 支持自定义 Skill 配置（YAML）
- [ ] 代码覆盖率报告（pytest-cov）
- [ ] 有界任务队列 + 拒绝策略（防止无限排队）
- [ ] 动态并发调整（根据系统负载自动调整）

## 📄 许可证

MIT License - 详见 [LICENSE](LICENSE) 文件

## 🙏 致谢

- 灵感来源于 **DeerFlow 2.0** 的多智能体协作架构
- 使用 **OpenAI 兼容 API** 实现 LLM 能力
- **飞书开放平台** 提供 ChatOps 接口
- **Docker** 提供轻量级容器隔离

---

**注意事项**：
- 本项目为原型验证系统，生产使用前请充分测试
- API 调用会产生费用，请注意控制
- 建议在隔离环境（非生产环境）首次部署

---

## 🔗 相关链接

- [飞书开发者文档](https://open.feishu.cn/document/)
- [OpenAI API 文档](https://platform.openai.com/docs/api-reference)
- [Docker 官方文档](https://docs.docker.com/)
- [并发安全修复报告](CONCURRENCY_FIXES.md) - 详细的线程安全修复说明
