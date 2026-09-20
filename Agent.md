# Agent.md — AIO LangGraph Demo 项目说明

面向 AI 编码助手与开发者的项目上下文文档。使用说明见 [README.md](README.md)。

## 项目是什么

LangGraph/DeepAgents 智能体 + AIO Sandbox（Docker 容器，HTTP API 在 `localhost:8080`）的集成演示。
Agent 角色：**市场调研分析师**，在沙箱内调研并产出报告；提供 CLI 与 Web UI 两种形态，共享同一套构建逻辑。

## 文件地图

| 文件 | 职责 |
|------|------|
| `agent_core.py` | `SYSTEM_PROMPT`、`DEFAULT_TASK`、`create_client()`、`build_model()`、`open_backend()`、`create_agent()` —— CLI 与 Web 共用的唯一事实源 |
| `sandbox_backend.py` | `AIOSandboxBackend`：实现 DeepAgents `BaseSandbox` 协议，把 execute/读写文件转发到沙箱；构造时打开持久 shell 会话（阻塞网络调用），用完必须 `close()` |
| `main.py` | CLI 入口：流式打印 agent 执行过程 |
| `web_app.py` | FastAPI 后端（端口 7860）：后台任务 runs + SSE + 沙箱代理接口 |
| `static/index.html` | 单文件自包含前端：聊天 + 三标签面板（浏览器/文件系统/日志追踪），无外部依赖 |

## Agent 执行链路

```
POST /api/runs {message} → asyncio.create_task(_execute_run)
  to_thread(open_backend) → create_agent → agent.astream(stream_mode="values")
  → 事件缓冲进 _Run.events（user/assistant/tool/error）→ SSE 回放给任意连接者
```

- 运行与 HTTP 连接解耦：断线不停止任务；`GET /api/runs/{id}/stream` 先回放后跟新，可多端接入。
- 停止 = `task.cancel()` → `CancelledError` → status `cancelled`；`finally` 中 `shield(backend.close)`。
- 状态机：`running → done | error | cancelled`（`cancelling` 为过渡态）。

## web_app.py API 一览

| 端点 | 说明 |
|------|------|
| `POST /api/runs` / `GET /api/runs/{id}/stream` / `POST /api/runs/{id}/stop` / `GET /api/runs` | 后台任务生命周期 |
| `GET /api/screenshot` | 沙箱桌面 PNG（`client.browser.screenshot()` 是 `Iterator[bytes]`，需聚合；`to_thread` 包装） |
| `GET /api/files?path=` | 列目录（`list_path`），**过滤隐藏项**（名字以 `.` 开头），目录在前按名排序 |
| `GET /api/file?path=` | 读文件（`read_file`，0–6000 行） |
| `POST /api/open {path}` | 沙箱内打开文件：`DISPLAY=:99.0 nohup /opt/browser/chrome --no-sandbox --new-window "file://…"`；path 必须在 `/home/gem/` 下并做字符清洗 |
| `GET /api/config` | sandbox_url + 默认任务文案 |

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

Agent 每次调研任务须产出两个文件：`/home/gem/report.md`（结构化报告）和 `/home/gem/report.html`（单文件精排报告，规范写在 SYSTEM_PROMPT 里）。Web UI 的文件树可直接预览二者，双击则用沙箱 Chrome 打开。

## 本地运行速查

```bash
# 沙箱（另开终端）
docker run --security-opt seccomp=unconfined --rm -it -p 127.0.0.1:8080:8080 \
  enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest
# 依赖 + Web UI
uv sync && uv run uvicorn web_app:app --port 7860   # http://localhost:7860
# CLI
uv run main.py "你的问题"
```

密钥在 `.env`（勿提交）：`OPENAI_BASEURL` / `OPENAI_API_KEY` / `OPENAI_MODEL_ID`。
