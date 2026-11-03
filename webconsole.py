"""手机网页控制台：纯 asyncio 的 HTTP + SSE 服务，无需第三方依赖。"""

import asyncio
import hmac
import json
import mimetypes
import uuid
from contextlib import suppress
from urllib.parse import parse_qs, urlparse

from common import (
    MAX_LINE,
    WEB_BIND,
    WEB_DIR,
    WEB_PORT,
    WEB_TOKEN,
    close_writer,
    local_ips,
    normalize_target,
)

REASONS = {
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    500: "Internal Server Error",
}

STATIC = {}  # path -> (bytes, content-type)
STATIC_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}


def port():
    return WEB_PORT


def load_static():
    STATIC.clear()
    for name, ctype in STATIC_TYPES.items():
        path = WEB_DIR / name
        if path.exists():
            STATIC["/" + name] = (path.read_bytes(), ctype)
    if "/index.html" in STATIC:
        STATIC["/"] = STATIC["/index.html"]


def _json(data):
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def static_file(path):
    """从 web 目录读取静态文件；只允许目录下的单层文件名，防止目录穿越。"""
    if path in STATIC:
        return STATIC[path]
    name = path.lstrip("/")
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    file_path = WEB_DIR / name
    if not file_path.is_file():
        return None
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype == "application/javascript":
        ctype += "; charset=utf-8"
    try:
        return file_path.read_bytes(), ctype
    except OSError:
        return None


async def respond(writer, status, body=b"", ctype="application/json; charset=utf-8"):
    head = (
        f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}\r\n"
        f"Content-Type: {ctype}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "Cache-Control: no-store\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "\r\n"
    )
    writer.write(head.encode("latin-1") + body)
    await writer.drain()


async def read_http(reader):
    """读取一个 HTTP 请求，返回 (method, path, headers, body)。"""
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    except (
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
        asyncio.TimeoutError,
        ConnectionError,
        ValueError,
    ):
        return None
    if len(head) > 16 * 1024:
        return None
    try:
        request_line, *header_lines = head.decode("latin-1").split("\r\n")
        method, target, _ = request_line.split(" ", 2)
    except ValueError:
        return None
    headers = {}
    for line in header_lines:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    try:
        length = int(headers.get("content-length") or 0)
    except ValueError:
        length = 0
    if length < 0 or length > MAX_LINE:
        return None
    body = b""
    if length:
        try:
            body = await asyncio.wait_for(reader.readexactly(length), 10)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            return None
    return method.upper(), target, headers, body


def token_ok(headers, query):
    if not WEB_TOKEN:
        return True  # 空令牌表示关闭认证（仅建议内网调试使用）
    supplied = headers.get("x-auth-token") or (
        query.get("token", [""])[0] if query else ""
    )
    return hmac.compare_digest(supplied or "", WEB_TOKEN)


async def sse_send(writer, obj):
    writer.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode())
    await writer.drain()


async def stream_events(reader, writer, hub, query):
    """SSE：浏览器保持长连接，服务器实时推送状态、回执和上下线事件。"""
    client_id = (query.get("client_id") or ["browser"])[0][:64] or "browser"
    queue = asyncio.Queue(maxsize=200)
    hub.web_clients[client_id] = queue

    writer.write(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream; charset=utf-8\r\n"
        b"Cache-Control: no-cache\r\n"
        b"Connection: keep-alive\r\n"
        b"X-Accel-Buffering: no\r\n"
        b"\r\n"
    )
    await writer.drain()
    await sse_send(writer, {"type": "hello", "status": hub.status()})
    try:
        while True:
            if hub.web_clients.get(client_id) is not queue:
                break  # 同一浏览器开了新页面，旧连接退出
            try:
                msg = await asyncio.wait_for(queue.get(), 20)
            except asyncio.TimeoutError:
                writer.write(b": ping\n\n")  # 心跳注释，保持连接
                await writer.drain()
                continue
            await sse_send(writer, msg)
    except (ConnectionError, OSError):
        pass
    finally:
        if hub.web_clients.get(client_id) is queue:
            hub.web_clients.pop(client_id, None)


async def api_command(writer, body, hub):
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        await respond(writer, 400, _json({"ok": False, "msg": "请求体不是合法 JSON"}))
        return
    if not isinstance(data, dict):
        await respond(writer, 400, _json({"ok": False, "msg": "请求体格式错误"}))
        return

    device_id = str(data.get("device_id") or "").strip()[:64]
    action = data.get("action")
    target_text = str(data.get("target") or "").strip()
    client_id = str(data.get("client_id") or "browser")[:64] or "browser"

    if action not in ("block", "unblock"):
        await respond(writer, 400, _json({"ok": False, "msg": "动作不合法"}))
        return
    if not device_id:
        await respond(writer, 400, _json({"ok": False, "msg": "缺少网关设备号"}))
        return
    kind, target = normalize_target(target_text)
    if kind is None:
        await respond(writer, 400, _json({"ok": False, "msg": target}))
        return
    if device_id not in hub.gateways:
        await respond(writer, 200, _json({"ok": False, "msg": "网关不在线"}))
        return

    req_id = uuid.uuid4().hex[:8]
    ok, err = await hub.submit(
        ("web", client_id), req_id, device_id, action, target
    )
    if not ok:
        await respond(writer, 200, _json({"ok": False, "msg": err}))
        return
    await respond(
        writer,
        200,
        _json({"ok": True, "req_id": req_id, "msg": "指令已下发"}),
    )


async def handle(reader, writer, hub):
    try:
        req = await read_http(reader)
        if req is None:
            await respond(writer, 400, _json({"ok": False, "msg": "请求格式错误"}))
            return
        method, target, headers, body = req
        parsed = urlparse(target)
        path = parsed.path
        query = parse_qs(parsed.query)

        if method == "GET" and not path.startswith("/api/"):
            found = static_file(path)
            if found is not None:
                data, ctype = found
                await respond(writer, 200, data, ctype)
                return
        if path == "/favicon.ico":
            await respond(writer, 204)
            return
        if not path.startswith("/api/"):
            await respond(writer, 404, b"Not Found", "text/plain; charset=utf-8")
            return
        if not token_ok(headers, query):
            await respond(writer, 401, _json({"ok": False, "msg": "令牌无效"}))
            return

        if method == "GET" and path == "/api/status":
            await respond(writer, 200, _json(hub.status()))
        elif method == "GET" and path == "/api/history":
            await respond(
                writer, 200, _json({"ok": True, "history": list(hub.history)})
            )
        elif method == "POST" and path == "/api/command":
            await api_command(writer, body, hub)
        elif method == "GET" and path == "/api/events":
            await stream_events(reader, writer, hub, query)
        else:
            await respond(writer, 404, _json({"ok": False, "msg": "接口不存在"}))
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    except Exception as e:
        print(f"[WEB] 请求处理异常: {e}")
        with suppress(Exception):
            await respond(writer, 500, _json({"ok": False, "msg": "服务器内部错误"}))
    finally:
        await close_writer(writer)


async def serve(hub):
    load_static()
    if not STATIC:
        print(f"[WEB] 警告: 未找到网页文件，请检查 {WEB_DIR}")
    server = await asyncio.start_server(
        lambda reader, writer: handle(reader, writer, hub),
        WEB_BIND,
        WEB_PORT,
        limit=MAX_LINE,
    )
    print(
        f"[WEB] 网页控制台已启动 http://{WEB_BIND}:{WEB_PORT} "
        f"（默认令牌: {WEB_TOKEN or '未启用认证'}）"
    )
    for ip in local_ips():
        print(f"[WEB] 手机访问: http://{ip}:{WEB_PORT}")
    async with server:
        await server.serve_forever()
