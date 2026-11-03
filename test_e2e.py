"""端到端自动化测试：启动中转（含网页控制台）和网关，验证完整链路。

运行: python test_e2e.py
"""

import asyncio
import itertools
import json
import os
import subprocess
import sys
import time
from urllib.parse import quote

from common import BASE_DIR, close_writer, read_json, send_json

RELAY_PORT = 19000
WEB_PORT = 18080
WEB_TOKEN = "test-token"
GATEWAY_ID = "gw-test"
SILENT_ID = "gw-silent"
req_seq = itertools.count(1)
procs = []


def start(script, env_extra=None):
    env = os.environ.copy()
    env.update(
        {
            "RELAY_PORT": str(RELAY_PORT),
            "RELAY_HOST": "127.0.0.1",
            "WEB_PORT": str(WEB_PORT),
            "WEB_TOKEN": WEB_TOKEN,
        }
    )
    env.update(env_extra or {})
    proc = subprocess.Popen(
        [sys.executable, "-u", script],
        cwd=str(BASE_DIR),
        env=env,
        stdout=subprocess.DEVNULL,  # 避免管道写满导致子进程阻塞
        stderr=subprocess.DEVNULL,
    )
    procs.append(proc)
    return proc


def check(cond, text):
    if not cond:
        raise AssertionError(text)
    print(f"[PASS] {text}")


async def recv_until(reader, pred, timeout, what):
    deadline = time.monotonic() + timeout
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise AssertionError(f"等待超时: {what}")
        try:
            msg = await asyncio.wait_for(read_json(reader), remain)
        except asyncio.TimeoutError:
            raise AssertionError(f"等待超时: {what}")
        if msg is None:
            raise AssertionError(f"连接已断开: {what}")
        if msg and pred(msg):
            return msg


async def open_parent():
    for _ in range(50):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", RELAY_PORT)
            await send_json(writer, {"type": "parent"})
            msg = await asyncio.wait_for(read_json(reader), 2)
            if msg and msg.get("type") == "status":
                return reader, writer
        except OSError:
            await asyncio.sleep(0.1)
        except asyncio.TimeoutError:
            break
    raise AssertionError("中转未启动")


async def wait_gateway_online(reader, writer):
    for _ in range(50):
        await send_json(writer, {"type": "list"})
        msg = await asyncio.wait_for(read_json(reader), 2)
        if msg and GATEWAY_ID in (msg.get("gateways") or []):
            return
        await asyncio.sleep(0.2)
    raise AssertionError("网关未上线")


async def run_cmd(reader, writer, action, target, device=GATEWAY_ID):
    req_id = f"t{next(req_seq)}"
    await send_json(
        writer,
        {
            "type": "cmd",
            "req_id": req_id,
            "device_id": device,
            "payload": {"action": action, "target": target},
        },
    )
    return await recv_until(
        reader,
        lambda m: m.get("type") == "result" and m.get("req_id") == req_id,
        5,
        f"{action} {target} 的回执",
    )


# ---------- 网页控制台测试工具 ----------


async def http(method, path, token=None, body=None, timeout=5):
    reader, writer = await asyncio.open_connection("127.0.0.1", WEB_PORT)
    headers = [
        f"{method} {path} HTTP/1.1",
        "Host: 127.0.0.1",
        "Connection: close",
    ]
    if token is not None:
        headers.append(f"X-Auth-Token: {token}")
    payload = b""
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers.append("Content-Type: application/json")
        headers.append(f"Content-Length: {len(payload)}")
    writer.write(("\r\n".join(headers) + "\r\n\r\n").encode() + payload)
    await writer.drain()
    try:
        raw = await asyncio.wait_for(reader.read(), timeout)
    finally:
        await close_writer(writer)
    head, _, data = raw.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    return status, data


async def open_sse(client_id):
    reader, writer = await asyncio.open_connection("127.0.0.1", WEB_PORT)
    request = (
        f"GET /api/events?token={quote(WEB_TOKEN)}&client_id={quote(client_id)} "
        "HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n"
    )
    writer.write(request.encode())
    await writer.drain()
    while True:
        line = await asyncio.wait_for(reader.readline(), 5)
        if line in (b"\r\n", b"\n", b""):
            break
    return reader, writer


async def recv_sse(reader, pred, timeout, what):
    deadline = time.monotonic() + timeout
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise AssertionError(f"等待超时: {what}")
        try:
            line = await asyncio.wait_for(reader.readline(), remain)
        except asyncio.TimeoutError:
            raise AssertionError(f"等待超时: {what}")
        if not line:
            raise AssertionError(f"SSE 连接已断开: {what}")
        if line.startswith(b"data: "):
            msg = json.loads(line[6:])
            if pred(msg):
                return msg


async def wait_blocked(site, present=True, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, data = await http("GET", "/api/status", token=WEB_TOKEN)
        if status == 200:
            devices = json.loads(data).get("devices") or []
            device = next((d for d in devices if d["id"] == GATEWAY_ID), None)
            if device and ((site in device["blocked"]) == present):
                return True
        await asyncio.sleep(0.2)
    raise AssertionError(f"网页状态未更新: {site} present={present}")


# ---------- 测试主流程 ----------


async def web_tests():
    status, data = await http("GET", "/api/status")
    check(status == 401, "网页接口未带令牌返回 401")

    status, data = await http("GET", "/")
    check(
        status == 200 and "护苗" in data.decode("utf-8"),
        "网页控制台静态页面可访问",
    )

    status, data = await http("GET", "/api/status", token=WEB_TOKEN)
    check(status == 200, "带令牌可以访问状态接口")
    check(
        any(d["id"] == GATEWAY_ID for d in json.loads(data).get("devices", [])),
        "网页控制台能看到在线网关",
    )

    sse_reader, sse_writer = await open_sse("e2e-browser")
    hello = await recv_sse(
        sse_reader, lambda m: m.get("type") == "hello", 5, "SSE 欢迎消息"
    )
    check(
        hello.get("status", {}).get("devices"),
        "SSE 建连后推送当前网关状态",
    )

    status, data = await http(
        "POST",
        "/api/command",
        token=WEB_TOKEN,
        body={
            "device_id": GATEWAY_ID,
            "action": "block",
            "target": "7.7.7.7",
            "client_id": "e2e-browser",
        },
    )
    resp = json.loads(data)
    check(status == 200 and resp.get("ok") and resp.get("req_id"), "网页端下发阻断指令")

    result = await recv_sse(
        sse_reader,
        lambda m: m.get("type") == "result" and m.get("req_id") == resp["req_id"],
        5,
        "网页端阻断回执",
    )
    check(
        result["payload"]["ok"] and "已阻断" in result["payload"]["msg"],
        "网页端通过 SSE 收到阻断回执",
    )
    await wait_blocked("7.7.7.7", True)
    print("[PASS] 网页端管制清单实时更新")

    status, data = await http("GET", "/api/history", token=WEB_TOKEN)
    rows = json.loads(data).get("history") or []
    check(
        any(r.get("target") == "7.7.7.7" and r.get("ok") for r in rows),
        "网页端可以查询执行记录",
    )

    status, data = await http(
        "POST",
        "/api/command",
        token=WEB_TOKEN,
        body={
            "device_id": GATEWAY_ID,
            "action": "block",
            "target": "bad_target!!",
            "client_id": "e2e-browser",
        },
    )
    resp = json.loads(data)
    check(status == 400 and not resp.get("ok"), "网页端拒绝非法管控目标")

    status, data = await http(
        "POST",
        "/api/command",
        token=WEB_TOKEN,
        body={
            "device_id": GATEWAY_ID,
            "action": "unblock",
            "target": "7.7.7.7",
            "client_id": "e2e-browser",
        },
    )
    check(json.loads(data).get("ok"), "网页端下发解除指令")
    await wait_blocked("7.7.7.7", False)
    print("[PASS] 网页端解除后管制清单同步移除")

    await close_writer(sse_writer)


async def main():
    start("relay.py", {"HEARTBEAT_INTERVAL": "1", "HEARTBEAT_TIMEOUT": "3"})
    await asyncio.sleep(0.3)
    reader, writer = await open_parent()
    gateway = start(
        "gateway.py", {"DEVICE_ID": GATEWAY_ID, "HEARTBEAT_INTERVAL": "1"}
    )
    try:
        await wait_gateway_online(reader, writer)
        print("[PASS] 网关注册后家长端能看到在线设备")

        r = await run_cmd(reader, writer, "block", "1.2.3.4")
        check(r["payload"]["ok"] and "已阻断" in r["payload"]["msg"], "阻断指令执行成功")

        r = await run_cmd(reader, writer, "block", "1.2.3.4")
        check(r["payload"]["ok"] and "已处于阻断" in r["payload"]["msg"], "重复阻断是幂等的")

        r = await run_cmd(reader, writer, "unblock", "1.2.3.4")
        check(r["payload"]["ok"] and "已解除" in r["payload"]["msg"], "解除指令执行成功")

        r = await run_cmd(reader, writer, "unblock", "1.2.3.4")
        check(r["payload"]["ok"] and "无需解除" in r["payload"]["msg"], "重复解除是幂等的")

        r = await run_cmd(reader, writer, "block", "not_an_ip!")
        check(not r["payload"]["ok"], "非法管控目标被拒绝")

        r = await run_cmd(reader, writer, "block", "1.2.3.4", device="ghost")
        check(not r["payload"]["ok"] and "不在线" in r["payload"]["msg"], "离线网关返回失败")

        # 非家长端连接不能下发指令
        raw_r, raw_w = await asyncio.open_connection("127.0.0.1", RELAY_PORT)
        await send_json(
            raw_w,
            {
                "type": "cmd",
                "req_id": "raw1",
                "device_id": GATEWAY_ID,
                "payload": {"action": "block", "target": "5.6.7.8"},
            },
        )
        m = await recv_until(
            raw_r, lambda x: x.get("type") == "result", 5, "未授权连接的拒绝回执"
        )
        check(not m["payload"]["ok"], "未授权连接不能下发指令")
        await close_writer(raw_w)

        await web_tests()

        # 注册后不发心跳的网关，应被中转判定超时并通知家长端
        silent_r, silent_w = await asyncio.open_connection("127.0.0.1", RELAY_PORT)
        await send_json(silent_w, {"type": "register", "device_id": SILENT_ID})
        await recv_until(
            reader,
            lambda x: x.get("type") == "event"
            and x.get("event") == "gateway_online"
            and x.get("device_id") == SILENT_ID,
            5,
            "静默网关上线上报",
        )
        await recv_until(
            reader,
            lambda x: x.get("type") == "event"
            and x.get("event") == "gateway_offline"
            and x.get("device_id") == SILENT_ID
            and x.get("reason") == "heartbeat_timeout",
            10,
            "静默网关心跳超时下线",
        )
        print("[PASS] 心跳超时的网关被清理并通知家长端")
        await close_writer(silent_w)
        await close_writer(silent_r)

        # 关闭真实网关，家长端应收到下线事件
        gateway.terminate()
        gateway.wait(timeout=5)
        await recv_until(
            reader,
            lambda x: x.get("type") == "event"
            and x.get("event") == "gateway_offline"
            and x.get("device_id") == GATEWAY_ID,
            10,
            "网关断开下线事件",
        )
        print("[PASS] 网关断开后家长端收到下线通知")

        await send_json(writer, {"type": "list"})
        m = await recv_until(
            reader, lambda x: x.get("type") == "status", 5, "在线列表"
        )
        check(m.get("gateways") == [], "所有网关下线后在线列表为空")

        print("\n全部测试通过")
    finally:
        await close_writer(writer)


def cleanup():
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    try:
        asyncio.run(main())
        code = 0
    except AssertionError as e:
        print(f"\n[FAIL] {e}")
        code = 1
    except Exception:
        import traceback

        traceback.print_exc()
        code = 1
    finally:
        cleanup()
    sys.exit(code)
