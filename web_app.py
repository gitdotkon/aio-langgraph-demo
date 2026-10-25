"""FastAPI Web UI for the AIO Sandbox DeepAgents demo.

Exposes the agent as a browser-based chat interface with:
  * POST /api/chat      -> Server-Sent Events stream of the agent run
                           (AI text, tool calls and tool results)
  * GET  /api/screenshot -> the AIO Sandbox browser/desktop screen as PNG
  * GET  /               -> the single-page UI (static/index.html)

Run:
    uvicorn web_app:app --reload --port 7860
then open http://localhost:7860
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

load_dotenv()

from agent_core import (  # noqa: E402
    SANDBOX_WORKING_DIR,
    create_agent,
    create_client,
    default_task,
    local_workspace,
    open_backend,
    sandbox_reachable,
    sandbox_url,
)

STATIC_DIR = Path(__file__).parent / "static"


def _working_dir() -> str:
    """当前模式对应的工作目录（沙箱 /home/gem 或本地 local_workspace）。"""
    return SANDBOX_WORKING_DIR if sandbox_reachable() else str(local_workspace())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One stateless SDK client is shared across requests; the sandbox shell
    # session is created per chat run (see /api/chat) to avoid idle timeouts.
    app.state.client = create_client()
    app.state.sandbox_url = sandbox_url()
    mode = "sandbox" if sandbox_reachable() else "local"
    print(f"[runtime] 沙箱可达性探测: mode={mode} (url={app.state.sandbox_url})")
    yield
    # Nothing to tear down on the client; per-run backends close themselves.


app = FastAPI(title="AIO DeepAgents UI", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str


class OpenRequest(BaseModel):
    path: str


class _Run:
    """A background agent execution decoupled from any HTTP connection.

    Events accumulate in `events` so a client can disconnect (background) and
    later reconnect and replay them. `task.cancel()` interrupts the agent.
    """

    __slots__ = ("id", "message", "events", "status", "task")

    def __init__(self, run_id: str, message: str):
        self.id = run_id
        self.message = message
        # The user's own message is the first event, so a reconnecting client
        # re-renders the full transcript (including it) from scratch.
        self.events: list[dict] = [{"role": "user", "content": message}]
        self.status = "running"  # running | done | error | cancelled
        self.task: asyncio.Task | None = None


RUNS: dict[str, _Run] = {}
MAX_RUNS = 20  # keep the in-memory buffer from growing unbounded


def _prune_runs() -> None:
    if len(RUNS) <= MAX_RUNS:
        return
    finished = [r for r in RUNS.values() if r.status != "running"]
    for r in sorted(finished, key=lambda x: x.id)[: len(RUNS) - MAX_RUNS]:
        RUNS.pop(r.id, None)


def _text_of(content) -> str:
    """Coerce LangChain message content (str | list[prompt parts]) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                parts.append(p.get("text") or p.get("content") or "")
        return "\n".join(x for x in parts if x)
    return str(content)


def _serialize_message(msg) -> dict | None:
    """Turn a LangChain message into a compact JSON event for the UI."""
    mtype = getattr(msg, "type", None)
    if mtype == "tool":
        return {
            "role": "tool",
            "name": getattr(msg, "name", "") or "tool",
            "content": _text_of(getattr(msg, "content", "")),
        }
    if mtype == "ai":
        tool_calls = []
        for tc in getattr(msg, "tool_calls", None) or []:
            tool_calls.append({
                "name": tc.get("name"),
                "args": tc.get("args"),
            })
        return {
            "role": "assistant",
            "content": _text_of(getattr(msg, "content", "")),
            "tool_calls": tool_calls,
        }
    return None


async def _execute_run(run: _Run):
    """Run the agent to completion in the background, buffering events."""
    client = app.state.client
    backend = None
    try:
        # open_backend 优先沙箱，连不通自动降级 LocalShellBackend（本地 runtime）
        backend = await asyncio.to_thread(open_backend, client)
        if getattr(backend, "mode", "sandbox") == "local":
            run.events.append({
                "role": "system",
                "content": f"沙箱不可达，本次任务已降级到本地 runtime（工作目录 {backend.working_dir}）",
            })
        agent = create_agent(backend)

        seen = 0
        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": run.message}]},
            stream_mode="values",
        ):
            msgs = chunk.get("messages", []) if isinstance(chunk, dict) else []
            for m in msgs[seen:]:
                event = _serialize_message(m)
                if event is not None:
                    run.events.append(event)
            seen = len(msgs)
            await asyncio.sleep(0)  # yield so cancellation between chunks is prompt
        if run.status == "running":
            run.status = "done"
    except asyncio.CancelledError:
        run.status = "cancelled"
    except Exception as exc:
        run.status = "error"
        run.events.append({"role": "error", "content": f"{type(exc).__name__}: {exc}"})
    finally:
        if backend is not None:
            # shield cleanup from a pending cancellation so the session closes;
            # LocalShellBackend 没有 close，用 getattr 保护
            close = getattr(backend, "close", None)
            if callable(close):
                try:
                    await asyncio.shield(asyncio.to_thread(close))
                except Exception:
                    pass
        _prune_runs()


@app.get("/api/config")
async def get_config():
    reachable = sandbox_reachable()
    mode = "sandbox" if reachable else "local"
    wd = _working_dir()
    return {
        "sandbox_url": app.state.sandbox_url,
        "sandbox_reachable": reachable,
        "mode": mode,
        "working_dir": wd,
        "default_task": default_task(wd),
    }


@app.post("/api/runs")
async def start_run(req: ChatRequest):
    """Start an agent run in the background and return its id immediately."""
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    run_id = uuid.uuid4().hex[:8]
    run = _Run(run_id, req.message)
    RUNS[run_id] = run
    run.task = asyncio.create_task(_execute_run(run))
    return {"run_id": run_id}


async def _stream_run(run_id: str):
    run = RUNS.get(run_id)
    if run is None:
        return
    idx = 0
    while True:
        while idx < len(run.events):
            ev = run.events[idx]
            idx += 1
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        if run.status != "running":
            yield f"event: end\ndata: {json.dumps({'status': run.status})}\n\n"
            return
        await asyncio.sleep(0.2)  # light polling; avoids multi-subscriber races


@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str):
    """Attach to a run: replay buffered events, then follow live. GET so the
    browser can use EventSource; disconnecting here does NOT stop the run."""
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail="no such run")
    return StreamingResponse(
        _stream_run(run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/runs/{run_id}/stop")
async def stop_run(run_id: str):
    """Interrupt (cancel) a running agent task."""
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="no such run")
    if run.task and not run.task.done():
        run.status = "cancelling"
        run.task.cancel()
    return {"run_id": run_id, "status": run.status}


@app.get("/api/runs")
async def list_runs():
    return {
        "runs": [
            {
                "id": r.id,
                "status": r.status,
                "message": (r.message or "")[:60],
                "events": len(r.events),
            }
            for r in RUNS.values()
        ]
    }


@app.get("/api/screenshot")
async def screenshot():
    """Return the current AIO Sandbox browser/desktop screen as a PNG."""
    if not sandbox_reachable():
        raise HTTPException(status_code=503, detail="本地模式：无沙箱浏览器画面，截图不可用")
    client = app.state.client

    def _grab() -> bytes:
        return b"".join(client.browser.screenshot())

    try:
        png = await asyncio.to_thread(_grab)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"screenshot unavailable: {exc}") from exc
    if not png:
        raise HTTPException(status_code=503, detail="empty screenshot")
    return Response(content=png, media_type="image/png")


@app.get("/api/files")
async def list_files(path: str | None = None):
    """List a working directory: sandbox file API, or local scandir when degraded."""
    if not sandbox_reachable():
        root = local_workspace()
        target = Path(path).resolve() if path else root
        if not target.is_relative_to(root):
            raise HTTPException(status_code=400, detail="path must be inside the local workspace")

        def _ls_local() -> list[dict]:
            items = []
            try:
                entries = list(os.scandir(target))
            except OSError:
                return items
            for e in entries:
                # 只显示 Agent 生成的产物：跳过以点开头的隐藏文件/目录
                if e.name.startswith("."):
                    continue
                stat = e.stat()
                items.append({
                    "name": e.name,
                    "path": e.path,
                    "is_dir": e.is_dir(),
                    "size": None if e.is_dir() else stat.st_size,
                    "modified_at": stat.st_mtime,
                })
            items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            return items

        try:
            return {"path": str(target), "files": await asyncio.to_thread(_ls_local)}
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"list failed: {exc}") from exc

    client = app.state.client
    path = path or SANDBOX_WORKING_DIR

    def _ls() -> list[dict]:
        resp = client.file.list_path(path=path, include_size=True)
        items = []
        for f in getattr(resp.data, "files", None) or []:
            full = f.path
            name = full.rstrip("/").rsplit("/", 1)[-1] or full
            # 只显示 Agent 生成的产物：跳过以点开头的隐藏文件/系统目录
            if name.startswith("."):
                continue
            items.append({
                "name": name,
                "path": full,
                "is_dir": bool(getattr(f, "is_directory", False)),
                "size": getattr(f, "size", None),
                "modified_at": getattr(f, "modified_time", None),
            })
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return items

    try:
        return {"path": path, "files": await asyncio.to_thread(_ls)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"list failed: {exc}") from exc


@app.get("/api/file")
async def read_file(path: str):
    """Read a file's text content: sandbox file API, or local disk when degraded."""
    if not sandbox_reachable():
        root = local_workspace()
        target = Path(path).resolve()
        if not target.is_relative_to(root):
            raise HTTPException(status_code=400, detail="path must be inside the local workspace")

        def _read_local() -> str:
            return target.read_text(encoding="utf-8", errors="replace")[:200_000]

        try:
            return {"path": path, "content": await asyncio.to_thread(_read_local)}
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"read failed: {exc}") from exc

    client = app.state.client

    def _read() -> str:
        resp = client.file.read_file(file=path, start_line=0, end_line=6000)
        return getattr(resp.data, "content", "") or ""

    try:
        return {"path": path, "content": await asyncio.to_thread(_read)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"read failed: {exc}") from exc


@app.post("/api/open")
async def open_in_sandbox(req: OpenRequest):
    """Open a file for preview.

    sandbox 模式：用沙箱自己的 Chrome 在桌面（DISPLAY :99）打开，结果体现在
    “浏览器”页签的 /api/screenshot 回显里；
    local 模式：用宿主机默认浏览器打开 file:// 地址（沙箱桌面不可用）。
    """
    path = req.path or ""
    if not sandbox_reachable():
        root = local_workspace()
        target = Path(path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(status_code=400, detail="path must be a file inside the local workspace")
        opened = await asyncio.to_thread(webbrowser.open, target.as_uri())
        return {"ok": opened, "path": str(target), "detail": "opened in host browser (local mode)"}

    if not path.startswith(SANDBOX_WORKING_DIR + "/"):
        raise HTTPException(status_code=400, detail="path must be inside the sandbox working dir")
    # crude sanitize: keep it a plain filesystem path
    safe = path.replace('"', "").replace("`", "").replace(";", "").replace("$", "")
    display = os.getenv("SANDBOX_DISPLAY", ":99.0")
    client = app.state.client

    def _open() -> str:
        s = client.shell.create_session(exec_dir=SANDBOX_WORKING_DIR)
        sid = s.data.session_id
        try:
            cmd = (
                f"DISPLAY={display} nohup /opt/browser/chrome --no-sandbox "
                f"--new-window \"file://{safe}\" >/tmp/aio_open.log 2>&1 & echo opened"
            )
            r = client.shell.exec_command(command=cmd, id=sid, timeout=30)
            return getattr(r.data, "output", "") or ""
        finally:
            try:
                client.shell.cleanup_session(session_id=sid)
            except Exception:
                pass

    try:
        out = await asyncio.to_thread(_open)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"open failed: {exc}") from exc
    return {"ok": True, "path": path, "detail": out.strip()}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
