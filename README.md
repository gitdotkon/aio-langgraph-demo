# AIO Sandbox + LangGraph (DeepAgents) 集成示例

基于字节跳动开源 [AIO Sandbox](https://github.com/agent-infra/sandbox) 官方示例整理。
用 LangGraph 生态的 DeepAgents 创建深度智能体，AIO Sandbox 作为代码执行后端。

> 项目上下文 / API / 沙箱环境事实与约定见 [Agent.md](Agent.md)。

## 架构

```
DeepAgents Agent (LangGraph)
    └── AIOSandboxBackend (sandbox_backend.py)   # 实现 BaseSandbox 协议
        ├── execute(cmd)       → client.shell.exec_command()
        ├── upload_files()     → client.file.write_file()
        └── download_files()   → client.file.download_file()
            └── agent-sandbox SDK → AIO Sandbox HTTP API (konmini.local:8080)
```

Agent 获得 write / edit / read / ls / glob / execute 等工具，
所有操作都发生在 AIO Sandbox 的 Docker 容器里，与宿主机隔离。

> **运行时降级**：默认使用沙箱。启动时 `startup_check()` 会显式探测一次沙箱可达性并在日志中
> 打印模式与原因；之后 `agent_core.open_backend()` 每次先 HTTP 探测
> `SANDBOX_URL`（超时可用 `SANDBOX_PROBE_TIMEOUT` 调整，结果缓存 15s），
> 沙箱连不通时自动降级为本地 runtime（deepagents `LocalShellBackend`），
> 工作目录切换到项目下 `./local_workspace/`，提示词随之调整：
> 浏览器画面回显/沙箱内打开等沙箱专属能力在本地模式不可用（文件预览改走本地盘、宿主机浏览器）。
> ⚠️ 本地模式**无隔离**，agent 以本机用户权限执行命令，仅在可信环境使用。

提供两种运行形态，共享同一套 Agent 逻辑（`agent_core.py`）：

```
agent_core.py  # 系统提示词 / 模型 / Agent 构建（CLI 与 Web UI 共用）
├── main.py     # 命令行形态：流式打印执行过程
└── web_app.py  # Web 形态：FastAPI + 聊天界面 + 沙箱浏览器画面回显
    └── static/index.html
```

## 运行步骤

### 1. 启动 AIO Sandbox（需要 Docker）

> 本项目当前接入局域网机器 `konmini.local` 上已运行的沙箱
> （`.env` 中 `SANDBOX_URL=http://konmini.local:8080`）。
> 若要换回本机容器，用下面任一命令启动，并把 `SANDBOX_URL` 改回 `http://localhost:8080`。

国内用户：

```bash
docker run --security-opt seccomp=unconfined --rm -it -p 127.0.0.1:8080:8080 \
  enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest
```

国际用户：

```bash
docker run --security-opt seccomp=unconfined --rm -it -p 127.0.0.1:8080:8080 \
  ghcr.io/agent-infra/sandbox:latest
```

启动后浏览器打开 http://konmini.local:8080（本机容器则用 http://localhost:8080）
可看到沙箱控制台（终端 / 浏览器 / VSCode）。

### 2. 安装依赖（需要 Python 3.12+，推荐 uv）

```bash
cd aio-langgraph-demo
uv venv
uv sync
```

### 3. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入任意 OpenAI 兼容接口：
# - OpenRouter:  OPENAI_BASEURL=https://openrouter.ai/api/v1
# - 火山方舟:    OPENAI_BASEURL=https://ark.cn-beijing.volces.com/api/v3
#   MODEL_ID 形如 ep-xxxx 或 doubao-xxx
# - DeepSeek:    OPENAI_BASEURL=https://api.deepseek.com/v1
```

### 4. 运行

命令行形态：

```bash
uv run main.py
# 或直接带问题运行：
uv run main.py "用 Python 爬取一个网页标题并统计词频"
```

Web 交互界面形态（聊天 + 沙箱浏览器画面实时回显）：

```bash
uv run uvicorn web_app:app --reload --port 7860
# 浏览器打开 http://localhost:7860

# 局域网内其他设备访问（手机/平板）：需显式绑定 0.0.0.0，uvicorn 默认只监听 127.0.0.1
uv run uvicorn web_app:app --host 0.0.0.0 --port 7860
# 同一 Wi-Fi 下打开 http://<本机局域网IP>:7860
```

- 左侧输入任务，实时流式显示 Agent 回复、工具调用与返回结果；沙箱不可达时会自动降级本地 runtime，消息流里会显示系统提示。
- **先 Plan 后执行**：Agent 第一条回复先列出编号执行计划，随后不等确认自动逐步执行；中间不复述执行细节（工具调用/返回收进单张可折叠的「🛠 执行细节」卡），每步一行状态标记，最后简短总结与产物路径。
- 右侧点击「开始」轮询回显沙箱内浏览器 / 桌面截图（`/api/screenshot`，仅沙箱模式可用，本地模式显示降级提示）。
- 任务运行在服务端后台（`/api/runs`），支持**停止（打断）**、**后台执行**（断开后可从任务芯片重新接入回放），互不干扰。
- **对话在同一会话内累积上下文**：多轮消息共享历史（LangGraph checkpointer，服务端 session），不是每次发送都开新对话；点输入区「新会话」可清空上下文重新开始。

默认任务：调研 AI 编程助手市场，生成 `/home/gem/report.md` 与单文件 `/home/gem/report.html`。

## 自定义

- 改提问：`uv run main.py "你的问题"`，或在 Web 界面输入框直接发送
- 改系统提示词 / 默认任务：`agent_core.py` 中的 `_SYSTEM_PROMPT`、`_DEFAULT_TASK`（模板，`{WORKDIR}` 为工作目录占位）
- 换模型：`.env` 中的 `OPENAI_MODEL_ID`
- 降级工作区：`.env` 中的 `LOCAL_WORKSPACE_DIR`（默认 `./local_workspace`）

## 参考链接

- 官方仓库: https://github.com/agent-infra/sandbox
- 官方文档: https://sandbox.agent-infra.com/
- 本示例原目录: examples/langgraph-deepagents-integration
