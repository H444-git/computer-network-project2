"""单进程演示入口：中转 + 模拟网关 + 网页控制台同时运行。

用途：
- 本地一键演示：python demo_online.py，然后访问 http://127.0.0.1:8080
- 容器或只暴露一个 HTTP 端口的平台：本文件把 TCP 中转绑定在 127.0.0.1，
  网页监听 0.0.0.0:$PORT，网关在进程内连接本机中转。

注意：演示模式下的网关是模拟的，不会真正阻断本机流量。
真实三端部署仍使用 relay.py / gateway.py。
"""

import asyncio
import os

# 必须在导入 relay / gateway 之前设置，common.py 在导入时读取这些变量
os.environ.setdefault("RELAY_BIND", "127.0.0.1")
os.environ.setdefault("RELAY_HOST", "127.0.0.1")
os.environ.setdefault("RELAY_PORT", "9000")
os.environ.setdefault("WEB_BIND", "0.0.0.0")
os.environ.setdefault("WEB_PORT", os.environ.get("PORT", "8080"))
os.environ.setdefault("DEVICE_ID", "demo-gw")
os.environ.setdefault("TARGET_DEVICE", "demo-gw")

import gateway  # noqa: E402
import relay  # noqa: E402


async def main():
    print("[DEMO] 单进程演示模式：中转 + 模拟网关 + 网页控制台")
    await asyncio.gather(relay.main(), gateway.main())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[DEMO] 已退出")
