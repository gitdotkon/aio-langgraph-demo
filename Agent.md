# Agent.md — AIO LangGraph Demo 项目说明

面向 AI 编码助手与开发者的项目上下文文档。使用说明见 [README.md](README.md)。

> **维护约定**：本文件的每次修改都必须随当次改动一起提交 git（独立 commit 或与代码同 commit 均可），保证项目上下文与实现同步、可追溯。

## 项目是什么

LangGraph/DeepAgents 智能体 + AIO Sandbox（Docker 容器，当前跑在局域网机器
`konmini.local` 上，HTTP API 为 `http://konmini.local:8080`）的集成演示。
Agent 角色：**市场调研分析师**，在沙箱内调研并产出报告；提供 CLI 与 Web UI 两种形态，共享同一套构建逻辑。
**运行时选择：默认沙箱；`open_backend()` 先 HTTP 探测 `SANDBOX_URL`，连不通自动降级到本地 runtime（`LocalShellBackend`）。**

## 文件地图

| 文件 | 职责 |
|------|------|
| `agent_core.py` | 提示词模板（`build_system_prompt(workdir)` / `default_task(workdir)`，含**先 Plan 后执行协议**）、`sandbox_reachable()` 探测、`open_backend()` 运行时选择器（沙箱优先，降级 `LocalShellBackend`）、`build_model()`、`create_agent(backend, checkpointer=None)` —— CLI 与 Web 共用的唯一事实源 |
| `sandbox_backend.py` | `AIOSandboxBackend`：实现 DeepAgents `BaseSandbox` 协议，把 execute/读写文件转发到沙箱；构造时打开持久 shell 会话（阻塞网络调用），用完必须 `close()` |
| `main.py` | CLI 入口：流式打印 agent 执行过程 |
| `web_app.py` | FastAPI 后端（端口 7860）：后台任务 runs + SSE + 沙箱代理接口 |
| `static/index.html` | 单文件自包含前端：聊天 + 三标签面板（浏览器/文件系统/日志追踪），无外部依赖 |

## Agent 执行链路

```
POST /api/runs {message} → asyncio.create_task(_execute_run)
  to_thread(open_backend) → create_agent → agent.astream(stream_mode="values")
  → 事件缓冲进 _Run.events（user/system/assistant/tool/error）→ SSE 回放给任意连接者
```

- 运行与 HTTP 连接解耦：断线不停止任务；`GET /api/runs/{id}/stream` 先回放后跟新，可多端接入。
- **会话上下文：同一 session 内多轮对话共享历史**。全局 `MemorySaver` checkpointer + 服务端当前 `session_id` 作为 LangGraph `thread_id`，每次 run 都追加到同一 thread；`POST /api/session/new` 重置（换新 id + 新建 MemorySaver）。检查点仅内存态，服务重启后历史丢失。
- **先 Plan 后执行**：系统提示词内置执行协议——第一条回复只输出编号执行计划（不发工具消息），随后不等确认自动执行到底；中间回复不复述执行细节，每步最多一行「✅ 步骤 N 完成」，最后简短总结+产物路径。前端配套：工具调用/返回合并进单张可折叠的「🛠 执行细节」卡（每个 run 一张），聊天流只保留计划、步骤状态与总结。
- 降级发生时 run 事件流开头会插入一条 `role=system` 提示，前端以提示样式渲染。
- 停止 = `task.cancel()` → `CancelledError` → status `cancelled`；`finally` 中 `shield(backend.close)`（LocalShellBackend 无 close，用 getattr 保护）。
- 状态机：`running → done | error | cancelled`（`cancelling` 为过渡态）。

## web_app.py API 一览

| 端点 | 沙箱模式 | 本地降级模式 |
|------|----------|--------------|
| `POST /api/runs` / `GET /api/runs/{id}/stream` / `POST /api/runs/{id}/stop` / `GET /api/runs` | 后台任务生命周期（两种模式相同；body 可带 session_id 但服务端统一用当前会话） | 同左，事件流开头多一条 system 降级提示 |
| `POST /api/session/new` | 重置会话：返回新 `session_id`，清空 checkpointer（上一轮上下文不再携带） | 同左 |
| `GET /api/screenshot` | 沙箱桌面 PNG（`client.browser.screenshot()` 是 `Iterator[bytes]`，需聚合；`to_thread` 包装） | 直接 503（无沙箱画面） |
| `GET /api/files?path=` | 列目录（`list_path`），**过滤隐藏项**，目录在前按名排序 | `os.scandir(local_workspace)`，同构 JSON，path 限制在工作区内 |
| `GET /api/file?path=` | 读文件（`read_file`，0–6000 行） | `Path.read_text`（限工作区内） |
| `POST /api/open {path}` | 沙箱内 Chrome 打开：`DISPLAY=:99.0 nohup /opt/browser/chrome …`；path 须在 `/home/gem/` 下 | 宿主机默认浏览器打开 `file://`（`webbrowser.open`） |
| `GET /api/config` | `{sandbox_url, sandbox_reachable, mode, working_dir, default_task, session_id}` | mode=`local`，working_dir=本地工作区 |

## 运行时选择与本地降级（实测约定）

- 探测：`sandbox_reachable()` 用 httpx GET `SANDBOX_URL` 根路径，收到任意 HTTP 响应即视为可达；超时/连接失败视为不可达。结果缓存 15s（`SANDBOX_PROBE_TIMEOUT` 控制探测超时）。
- 降级后端：`deepagents.backends.LocalShellBackend(root_dir=./local_workspace, virtual_mode=False, inherit_env=True)`，并挂 `mode="local"` / `working_dir` 属性；`AIOSandboxBackend` 对应 `mode="sandbox"`。
- 提示词随之变：系统提示词中工作目录用 `{WORKDIR}` 占位，沙箱专属的浏览器截图段（Method 3）仅沙箱模式注入。
- 安全提示：本地模式**无隔离**，agent 以本机用户权限执行任意 shell 命令并能读写工作区文件，仅在可信任务/可信模型下使用。

## 沙箱环境事实（实测）

- 镜像：all-in-one-sandbox v1.0.0.156，Ubuntu 22.04 aarch64，用户 `gem`，工作目录 `/home/gem`。
- 桌面 `DISPLAY=:99.0`（Xvnc:5900/noVNC:6080）；Chrome 在 `/opt/browser/chrome`（CDP 9222 默认未启动）；编辑器只有 `code-server`（:8200）+ vim/nano，**无 xdg-open**。
- `client.browser_page.*` 高层 API 返回 404，只有 `browser.execute_action`（低层键鼠）和 `browser.screenshot` 可用。
- 该环境曾实测**无外网出口**（httpx Errno 101），Agent 会降级为凭模型知识写报告——这是环境限制，不是代码 bug。
- 端口占用：5900,6080,8080,8088,8091,8100,8200,8888（改端口/换沙箱前先看 `sandbox.get_context()`）。

## 前端约定（改 static/index.html 前必读）

- BEM + `c-` 前缀类名；主题走 `:root --c-*` 变量；单文件、无 CDN、内联 SVG 图标（与个人技能 `html-ui-snippets`/`web-page-generator` 同一套规范）。
- 布局高度链已修平：`.c-app__body` 用 `minmax(0,1fr)` 行 + 各滚动容器 `min-height:0`，底部输入区/状态条 `flex:none`；新增区块滚动容器时要沿用此模式，`overscroll-behavior:contain`。
- 左右栏宽度可拖拽：右栏宽度 = `--panel-w` CSS 变量（`#splitter` 指针拖拽，localStorage 记忆，≤900px 退化为上下堆叠）。
- JS 全部在一个 IIFE 内，无全局污染；markdown 渲染、代码高亮、目录树均为手写零依赖实现。

## 产物契约

Agent 每次调研任务须产出两个文件：`report.md`（结构化报告）和 `report.html`（单文件精排报告，规范写在系统提示词里）。目录随模式变：沙箱为 `/home/gem/`，本地降级为 `./local_workspace/`。Web UI 的文件树可直接预览二者，双击则用沙箱 Chrome（或本地模式下宿主机浏览器）打开。

## 本地运行速查

```bash
# 沙箱：当前实例在局域网机器 konmini.local 上；本机另起容器用下面的命令
docker run --security-opt seccomp=unconfined --rm -it -p 127.0.0.1:8080:8080 \
  enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest
# 依赖 + Web UI
uv sync && uv run uvicorn web_app:app --port 7860   # http://localhost:7860
# CLI
uv run main.py "你的问题"
```

密钥在 `.env`（勿提交）：`OPENAI_BASEURL` / `OPENAI_API_KEY` / `OPENAI_MODEL_ID`；沙箱地址与降级配置：`SANDBOX_URL` / `SANDBOX_PROBE_TIMEOUT` / `LOCAL_WORKSPACE_DIR`（`local_workspace/` 已加入 .gitignore）。
