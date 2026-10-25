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
Workflow for market research:
1. Use httpx/curl to fetch search results and web pages
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
    "使用浏览器搜索相关资料，先将调研报告保存到 {WORKDIR}/report.md，"
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
_PROBE_TTL = 15.0


def sandbox_reachable(url: str | None = None) -> bool:
    """HTTP 探测沙箱是否连通。

    收到任意 HTTP 响应（含 404/5xx）即视为可达；只有连接失败/超时/代理
    错误才视为不可达。结果缓存 15s，避免每个 Web 请求都探测一次。
    """
    global _probe_cache
    url = url or sandbox_url()
    now = time.monotonic()
    if _probe_cache is not None and _probe_cache[0] == url and now < _probe_cache[2]:
        return _probe_cache[1]
    timeout = float(os.getenv("SANDBOX_PROBE_TIMEOUT", "3"))
    try:
        httpx.get(url, timeout=timeout, follow_redirects=True)
        ok = True
    except httpx.HTTPError:
        ok = False
    _probe_cache = (url, ok, now + _PROBE_TTL)
    return ok


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
