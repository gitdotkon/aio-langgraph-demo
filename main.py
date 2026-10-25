import asyncio
import sys

from agent_core import create_agent, default_task, open_backend


async def main():
    # open_backend 优先连 AIO Sandbox，连不通自动降级本地 runtime
    backend = await asyncio.to_thread(open_backend)
    print(f"[runtime] mode={backend.mode} working_dir={backend.working_dir}")
    try:
        agent = create_agent(backend)

        # 命令行参数作为提问，否则使用默认任务
        prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else default_task(backend.working_dir)

        # 流式输出执行过程
        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": prompt}]},
            stream_mode="values"
        ):
            if "messages" in chunk:
                chunk["messages"][-1].pretty_print()
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    asyncio.run(main())
