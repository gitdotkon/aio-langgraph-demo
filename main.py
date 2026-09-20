import asyncio
import sys

from agent_core import DEFAULT_TASK, create_agent, open_backend, create_client


async def main():
    client = create_client()

    backend = open_backend(client)
    try:
        agent = create_agent(backend)

        # 命令行参数作为提问，否则使用默认任务
        prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else DEFAULT_TASK

        # 流式输出执行过程
        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": prompt}]},
            stream_mode="values"
        ):
            if "messages" in chunk:
                chunk["messages"][-1].pretty_print()
    finally:
        backend.close()


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    asyncio.run(main())
