"""Shared agent construction for both the CLI (main.py) and the Web UI (web_app.py).

Keeps the system prompt, model wiring and sandbox client creation in one place so
the command-line demo and the web interface behave identically.
"""

from __future__ import annotations

import os

from agent_sandbox import Sandbox
from deepagents import create_deep_agent
from langchain.chat_models.base import init_chat_model

from sandbox_backend import AIOSandboxBackend

# 确保访问 localhost 时绕过系统代理
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")

SYSTEM_PROMPT = """You are a market research analyst with sandbox access.
Your working directory is /home/gem. Write files there.

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

## Method 3: Browser screenshot (for visual verification)
```python
from agent_sandbox import Sandbox
client = Sandbox(base_url="http://localhost:8080")
# Low-level browser control (keyboard/mouse actions only)
client.browser.execute_action(request={"action_type": "HOTKEY", "keys": ["ctrl", "l"]})
client.browser.execute_action(request={"action_type": "TYPING", "text": "https://example.com"})
client.browser.execute_action(request={"action_type": "HOTKEY", "keys": ["enter"]})
import time; time.sleep(3)
result = client.browser.screenshot()
```

Workflow for market research:
1. Use httpx/curl to fetch search results and web pages
2. Extract and analyze content from multiple sources
3. Cross-reference data for accuracy
4. Write a structured market research report to /home/gem/report.md
5. Convert the report into a styled single-file HTML page and save it as
   /home/gem/report.html

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
- Always save BOTH files: /home/gem/report.md AND /home/gem/report.html"""

# CLI 与 UI 在未提供输入时使用的默认任务
DEFAULT_TASK = (
    "请对 AI 编程助手市场进行调研（如 Cursor、GitHub Copilot、Windsurf 等产品），"
    "包括市场规模、主要玩家、产品特点、定价策略和竞争格局。"
    "使用浏览器搜索相关资料，先将调研报告保存到 /home/gem/report.md，"
    "再按系统提示中的 HTML 规范生成排版精美的单文件报告，保存到 /home/gem/report.html。"
)


def sandbox_url() -> str:
    return os.getenv("SANDBOX_URL", "http://localhost:8080")


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


def open_backend(client: Sandbox) -> AIOSandboxBackend:
    """Create (but do not enter) a sandbox backend for the given client.

    Construction opens a persistent shell session, so this performs a blocking
    network call. In async contexts, run it via asyncio.to_thread().
    Call .close() when done.
    """
    return AIOSandboxBackend(client)


def create_agent(backend: AIOSandboxBackend):
    """创建 DeepAgents 深度智能体，AIO Sandbox 作为后端。"""
    return create_deep_agent(
        system_prompt=SYSTEM_PROMPT,
        model=build_model(),
        backend=backend,
    )
