"""Shared agent construction for both the CLI (main.py) and the Web UI (web_app.py).

Keeps the system prompt, model wiring and sandbox client creation in one place so
the command-line demo and the web interface behave identically.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
from agent_sandbox import Sandbox
from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain.chat_models.base import init_chat_model

from sandbox_backend import AIOSandboxBackend

# 确保访问沙箱（localhost / konmini.local）时绕过系统代理
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,konmini.local")

# 沙箱模式下的 Agent 工作目录
SANDBOX_WORKING_DIR = "/home/gem"

# 系统提示词模板：{WORKDIR} 替换为实际工作目录，{METHOD3} 仅沙箱模式注入
_SYSTEM_PROMPT = """You are a market research analyst with sandbox access.
Your working directory is {WORKDIR}. Write files there.

## 执行协议：先 Plan，后执行（严格遵守）
1. 收到任务后，第一条回复必须先输出执行计划：Markdown 编号列表（3–6 步，
   每步一句话）。这条计划回复里不得调用任何工具。
2. 输出计划后**不要等待用户确认**，下一轮立即开始按计划执行到底；
   可以连续多轮调用工具，但回复消息里**不要**复述命令、代码、
   网页内容等执行细节（工具过程本身会展示这些）。
3. 每个计划步骤完成后，最多用一行标记状态，如「✅ 步骤 2 完成：<几个词>」。
4. 全部步骤完成后输出简短总结：关键结论 + 产物文件路径；计划以外的细节
   不展开。

## A2UI：生成交互式界面（声明式 UI 子集协议）
当需要用户做选择、补充信息或确认方案时（例如执行前澄清需求范围），
优先输出可交互界面而不是纯文本提问：在回复中放一个 ```a2ui 围栏代码块，
内容是纯 JSON（只允许下列白名单组件，禁止 HTML/JS/样式代码）：

组件类型（均带 "type" 字段，文本字段全部是纯字符串）：
- {"type":"heading","text":".."}   标题
- {"type":"text","text":".."}      说明文字
- {"type":"status","state":"ok|warn|error","text":".."}   状态条
- {"type":"list","items":["第一项","第二项"]}   列表
- {"type":"table","columns":["A","B"],"rows":[["1","2"],["3","4"]]}   表格
- {"type":"button","label":"开始调研","action":"start","value":"按默认方案"}   动作按钮
- {"type":"form","action":"submit_scope","submitLabel":"提交并继续","fields":[
     {"name":"market","label":"目标市场","type":"text|textarea|select|checkbox",
      "options":["美股","A股"],"required":true,"placeholder":"可留空"}]}   表单

块格式：{"components":[ 组件1, 组件2, ... ]}（可解析 JSON，不超过 6 个组件、
表单不超过 5 个字段）。

规则：
1. 需要用户决策才能继续时，把 button/form 放进块后立即结束本轮回复等待交互；
   用户的交互会以「[A2UI 交互] action=… …」开头的用户消息回流，收到后按
   action/value 继续执行，不要重复询问。
2. 纯进度报告、最终总结不要用 A2UI，仍按执行协议的文本格式。
3. 不确定用户偏好时，宁可生成一个带默认选项的 form 让用户一键确认，
   也不要连发多条纯文本提问。

You can browse the web by fetching pages inside the sandbox.
Use the `execute` tool to run Python scripts or curl commands.

## Method 1: Python httpx (recommended for parsing)
```python
import httpx
from html.parser import HTMLParser

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.skip = False
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.skip = True
    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.skip = False
    def handle_data(self, data):
        if not self.skip:
            stripped = data.strip()
            if stripped:
                self.text.append(stripped)

resp = httpx.get("https://example.com", follow_redirects=True, timeout=15)
parser = TextExtractor()
parser.feed(resp.text)
print("\\n".join(parser.text[:100]))
```

## Method 2: curl (quick fetch)
```bash
curl -sL "https://example.com" | python3 -c "
import sys, re
html = sys.stdin.read()
text = re.sub(r'<(script|style)[^>]*>.*?</\\1>', '', html, flags=re.DOTALL)
text = re.sub(r'<[^>]+>', ' ', text)
text = re.sub(r'\\s+', ' ', text).strip()
print(text[:3000])
"
```

{METHOD3}
Workflow for market research（仍需先按上述执行协议输出计划）:
1. Fetch search results and web pages with httpx/curl
2. Extract and analyze content from multiple sources
3. Cross-reference data for accuracy
4. Write a structured market research report to {WORKDIR}/report.md
5. Convert the report into a styled single-file HTML page and save it as
   {WORKDIR}/report.html

## HTML report requirements (follow these conventions strictly)
- Single self-contained file: all CSS in one embedded <style> block;
  NO external CSS frameworks, JS libraries, font/icon CDNs.
- BEM class naming with a `c-` prefix to avoid style collisions
  (e.g. `.c-report__title`, `.c-report__section`, `.c-table--pricing`).
- Theme via CSS variables on `:root` (`--c-primary`, `--c-bg`, `--c-fg`,
  `--c-radius`, ...); cover all colors, radii and spacing.
- Semantic structure: header / nav (table of contents with anchor links) /
  main sections / pricing comparison tables / footer (source list + date).
- Accessible: visible :focus-visible styles, proper table markup
  (<table>, <th scope="col">), aria-label on icon-only controls.
- Mobile friendly: max-width container with relative units, responsive
  tables (overflow-x wrapper).
- Always save BOTH files: {WORKDIR}/report.md AND {WORKDIR}/report.html"""

# 沙箱模式专属：浏览器截图段（本地降级时没有沙箱桌面，整段移除）
_BROWSER_METHOD = """## Method 3: Browser screenshot (for visual verification)
```python
from agent_sandbox import Sandbox
# 在沙箱内运行的脚本访问自身 API 用 localhost；从宿主机访问才用 SANDBOX_URL
client = Sandbox(base_url="http://localhost:8080")
# Low-level browser control (keyboard/mouse actions only)
client.browser.execute_action(request={"action_type": "HOTKEY", "keys": ["ctrl", "l"]})
client.browser.execute_action(request={"action_type": "TYPING", "text": "https://example.com"})
client.browser.execute_action(request={"action_type": "HOTKEY", "keys": ["enter"]})
import time; time.sleep(3)
result = client.browser.screenshot()
```

"""

# CLI 与 UI 在未提供输入时使用的默认任务模板
_DEFAULT_TASK = (
    "请对 AI 编程助手市场进行调研（如 Cursor、GitHub Copilot、Windsurf 等产品），"
    "包括市场规模、主要玩家、产品特点、定价策略和竞争格局。"
    "使用浏览器搜索相关资料。先列出执行计划，给出计划后不等确认、直接按计划逐步执行（不要报告执行细节），"
    "先将调研报告保存到 {WORKDIR}/report.md，"
    "再按系统提示中的 HTML 规范生成排版精美的单文件报告，保存到 {WORKDIR}/report.html。"
)


def build_system_prompt(working_dir: str, *, sandbox_mode: bool = True) -> str:
    """按工作目录与运行模式生成系统提示词。"""
    prompt = _SYSTEM_PROMPT.replace("{WORKDIR}", working_dir)
    return prompt.replace("{METHOD3}", _BROWSER_METHOD if sandbox_mode else "")


def default_task(working_dir: str) -> str:
    return _DEFAULT_TASK.replace("{WORKDIR}", working_dir)


def local_workspace() -> Path:
    """降级模式的本地工作区（默认项目下 ./local_workspace，自动创建）。"""
    dir_ = os.getenv("LOCAL_WORKSPACE_DIR") or str(Path(__file__).parent / "local_workspace")
    root = Path(dir_).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


# (url, 结果, 过期时间戳)；只缓存默认 SANDBOX_URL 的探测结果
_probe_cache: tuple[str, bool, float] | None = None
# 最近一次真实探测失败的原因（不可达时供启动报告/UI 展示）
_probe_error: str | None = None
_PROBE_TTL = 15.0


def sandbox_reachable(url: str | None = None, *, force: bool = False) -> bool:
    """HTTP 探测沙箱是否连通。

    收到任意 HTTP 响应（含 404/5xx）即视为可达；只有连接失败/超时/代理
    错误才视为不可达。结果缓存 15s，避免每个 Web 请求都探测一次；
    启动检查等需要实时结果的场景传 force=True 跳过缓存。
    """
    global _probe_cache, _probe_error
    url = url or sandbox_url()
    now = time.monotonic()
    if (
        not force
        and _probe_cache is not None
        and _probe_cache[0] == url
        and now < _probe_cache[2]
    ):
        return _probe_cache[1]
    timeout = float(os.getenv("SANDBOX_PROBE_TIMEOUT", "3"))
    try:
        httpx.get(url, timeout=timeout, follow_redirects=True)
        ok, err = True, None
    except httpx.HTTPError as exc:
        ok, err = False, f"{type(exc).__name__}: {exc}"
    _probe_error = err
    _probe_cache = (url, ok, now + _PROBE_TTL)
    return ok


def startup_check() -> dict:
    """启动时显式探测沙箱可达性，返回结果并给出人话报告（CLI / Web 共用）。

    含阻塞网络调用（httpx），异步上下文请用 asyncio.to_thread()。
    """
    reachable = sandbox_reachable(force=True)
    url = sandbox_url()
    working_dir = SANDBOX_WORKING_DIR if reachable else str(local_workspace())
    if reachable:
        report = [f"[runtime] ✅ 沙箱可达: {url} → 沙箱模式（工作目录 {working_dir}）"]
    else:
        report = [
            f"[runtime] ⚠️ 沙箱不可达（{url}）: {_probe_error or '连接失败'}",
            f"[runtime]    → 已降级本地模式启动，工作目录 {working_dir}（无沙箱隔离；截图/沙箱桌面不可用）",
        ]
    return {
        "reachable": reachable,
        "mode": "sandbox" if reachable else "local",
        "working_dir": working_dir,
        "url": url,
        "error": _probe_error,
        "report": report,
    }


def sandbox_url() -> str:
    return os.getenv("SANDBOX_URL", "http://konmini.local:8080")


def create_client() -> Sandbox:
    """Create a stateless AIO Sandbox SDK client."""
    return Sandbox(base_url=sandbox_url())


def build_model():
    """通过 OpenAI 兼容接口接入任意模型（OpenRouter / 火山方舟 / DeepSeek 等）。"""
    return init_chat_model(
        model=f"openai:{os.getenv('OPENAI_MODEL_ID')}",
        base_url=os.getenv("OPENAI_BASEURL"),
        api_key=os.getenv("OPENAI_API_KEY"),
    )


def open_backend(client: Sandbox | None = None):
    """选择执行后端：优先 AIO Sandbox，连不通降级本地 runtime。

    - 沙箱可达：AIOSandboxBackend（mode="sandbox"，working_dir="/home/gem"）
    - 沙箱不可达：LocalShellBackend（mode="local"，working_dir=./local_workspace）

    两类后端都暴露 working_dir / mode 属性供调用方区分模式。
    沙箱路径是阻塞网络调用，异步上下文请用 asyncio.to_thread()；
    用完后调 .close()（LocalShellBackend 没有 close，请先用 getattr 保护）。
    """
    if sandbox_reachable():
        client = client or create_client()
        return AIOSandboxBackend(client, working_dir=SANDBOX_WORKING_DIR)
    root = local_workspace()
    backend = LocalShellBackend(
        root_dir=str(root),
        virtual_mode=False,
        inherit_env=True,
        timeout=120,
    )
    backend.mode = "local"
    backend.working_dir = str(root)
    return backend


def create_agent(backend, *, checkpointer=None):
    """创建 DeepAgents 深度智能体；系统提示词随后端模式（沙箱/本地）自动调整。

    传入 checkpointer（如 langgraph MemorySaver）并按 thread_id 调用时，
    同一会话的多轮对话会共享历史消息上下文；不传则每次运行无状态。
    """
    sandbox_mode = getattr(backend, "mode", "sandbox") == "sandbox"
    return create_deep_agent(
        system_prompt=build_system_prompt(backend.working_dir, sandbox_mode=sandbox_mode),
        model=build_model(),
        backend=backend,
        checkpointer=checkpointer,
    )
