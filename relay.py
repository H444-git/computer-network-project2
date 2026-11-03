"""中转服务器：维护网关与家长端连接，转发指令、路由回执，并提供网页控制台。"""

import asyncio
from contextlib import suppress

import webconsole
from common import (
    HEARTBEAT_TIMEOUT,
    MAX_LINE,
    RELAY_BIND,
    RELAY_PORT,
    close_writer,
    local_ips,
    normalize_target,
    now,
    read_json,
    send_json,
)
from hub import Hub

hub = Hub()


async def reply_result(writer, req_id, device_id, ok, text):
    if writer is None:
        return
    await send_json(
        writer,
        {
            "type": "result",
            "req_id": req_id,
            "device_id": device_id,
            "payload": {"ok": ok, "msg": text},
        },
    )


async def cleaner():
    """定期清理心跳超时的网关和过期的请求记录。"""
    while True:
        await asyncio.sleep(5)
        t = now()
        for did, gw in list(hub.gateways.items()):
            if t - gw["last_hb"] <= HEARTBEAT_TIMEOUT:
                continue
            # 超时：移出在线表并主动关闭 TCP，避免连接泄漏
            hub.gateways.pop(did, None)
            print(f"[RELAY] 心跳超时，清理网关 {did}")
            await close_writer(gw["writer"])
            await hub.event("gateway_offline", did, "heartbeat_timeout")
        hub.prune_pending()


async def handle(reader, writer):
    peer = writer.get_extra_info("peername")
    role, device_id = None, None
    try:
        while True:
            msg = await read_json(reader)
            if msg is None:
                break
            if not msg:
                continue  # 非法 JSON，忽略这条消息
            t = msg.get("type")

            if t == "register":
                did = str(msg.get("device_id") or "").strip()[:64]
                if not did:
                    await send_json(writer, {"type": "error", "msg": "缺少 device_id"})
                    continue
                old = hub.gateways.get(did)
                if old and old["writer"] is not writer:
                    # 同一设备重新注册：关闭旧连接，防止出现两个"在线"连接
                    print(f"[RELAY] 网关 {did} 重复注册，关闭旧连接")
                    await close_writer(old["writer"])
                first_time = old is None or old["writer"] is not writer
                role, device_id = "gateway", did
                hub.gateways[did] = {
                    "writer": writer,
                    "last_hb": now(),
                    "blocked": list(old.get("blocked") or []) if old else [],
                    "mode": old.get("mode", "") if old else "",
                }
                print(f"[RELAY] 网关注册 {did} from {peer}")
                await send_json(writer, {"type": "registered", "device_id": did})
                if first_time:
                    await hub.event("gateway_online", did)

            elif t == "parent":
                role = "parent"
                hub.parents[writer] = None
                print(f"[RELAY] 家长端接入 {peer}")
                await send_json(writer, hub.status())

            elif t == "heartbeat":
                did = msg.get("device_id")
                gw = hub.gateways.get(did)
                if gw and gw["writer"] is writer:
                    gw["last_hb"] = now()

            elif t == "state":
                # 网关上报当前管控状态（已阻断的目标、运行模式）
                gw = hub.gateways.get(msg.get("device_id"))
                if role != "gateway" or gw is None or gw["writer"] is not writer:
                    continue
                payload = msg.get("payload") or {}
                blocked = [
                    str(x)[:253]
                    for x in (payload.get("blocked") or [])
                    if isinstance(x, str)
                ][:100]
                mode = payload.get("mode")
                gw["blocked"] = blocked
                gw["mode"] = mode if mode in ("simulate", "iptables") else ""
                await hub.broadcast(
                    {
                        "type": "state",
                        "device_id": msg.get("device_id"),
                        "payload": {"blocked": blocked, "mode": gw["mode"]},
                    }
                )

            elif t == "cmd":
                if role != "parent":
                    await reply_result(
                        writer, msg.get("req_id"), msg.get("device_id"),
                        False, "未授权的连接不能下发指令",
                    )
                    continue
                req_id = msg.get("req_id")
                did = msg.get("device_id")
                payload = msg.get("payload") or {}
                action = payload.get("action")
                if action not in ("block", "unblock"):
                    await reply_result(writer, req_id, did, False, "未知指令")
                    continue
                kind, target = normalize_target(payload.get("target"))
                if kind is None:
                    await reply_result(writer, req_id, did, False, target)
                    continue
                ok, err = await hub.submit(
                    ("tcp", writer), req_id, did, action, target
                )
                if ok:
                    print(f"[RELAY] 转发指令 -> {did}: {action} {target}")
                else:
                    await reply_result(writer, req_id, did, False, err)

            elif t == "result":
                # 只接受已注册网关发来的回执，防止普通连接伪造结果
                gw = hub.gateways.get(msg.get("device_id"))
                if role != "gateway" or gw is None or gw["writer"] is not writer:
                    continue
                print(
                    f"[RELAY] 网关回执 {msg.get('device_id')}: {msg.get('payload')}"
                )
                await hub.deliver_result(msg)

            elif t == "list":
                await send_json(writer, hub.status())

    except Exception as e:
        print(f"[RELAY] 连接异常 {peer}: {e}")
    finally:
        if role == "gateway" and device_id:
            gw = hub.gateways.get(device_id)
            if gw is not None and gw["writer"] is writer:
                hub.gateways.pop(device_id, None)
                print(f"[RELAY] 网关下线 {device_id}")
                await hub.event("gateway_offline", device_id)
        hub.parents.pop(writer, None)
        await close_writer(writer)


async def run_web_console():
    try:
        await webconsole.serve(hub)
    except asyncio.CancelledError:
        raise
    except OSError as e:
        print(f"[WEB] 网页控制台启动失败: {e}")


async def main():
    cleaner_task = asyncio.create_task(cleaner())
    web_task = asyncio.create_task(run_web_console())
    server = await asyncio.start_server(
        handle, RELAY_BIND, RELAY_PORT, limit=MAX_LINE
    )
    print(f"[RELAY] 中继服务监听 {RELAY_BIND}:{RELAY_PORT}")
    for ip in local_ips():
        print(f"[RELAY] 手机控制台: http://{ip}:{webconsole.port()}  （同一局域网）")
    try:
        async with server:
            await server.serve_forever()
    finally:
        cleaner_task.cancel()
        web_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await web_task
        with suppress(asyncio.CancelledError, Exception):
            await cleaner_task


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[RELAY] 已退出")
