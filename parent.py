"""家长端：通过中转服务器向指定网关下发阻断/解除指令。"""

import asyncio
import uuid
from contextlib import suppress

from common import (
    CMD_TIMEOUT,
    MAX_LINE,
    RELAY_HOST,
    RELAY_PORT,
    TARGET_DEVICE,
    close_writer,
    normalize_target,
    read_json,
    send_json,
)

HELP = """\
命令列表:
  list / status      查看在线网关
  use <设备号>       选择要控制的网关（默认 {target}）
  block <IP|域名>    阻断该网址或 IP
  unblock <IP|域名>  解除阻断
  help               显示帮助
  quit               退出
"""


class ParentClient:
    def __init__(self):
        self.target = TARGET_DEVICE
        self.online = []
        self.reader = None
        self.writer = None
        self.pending = {}  # req_id -> {"action", "ip", "task"}
        self.recv_task = None
        self.closing = False

    async def connect(self):
        reader, writer = await asyncio.open_connection(
            RELAY_HOST, RELAY_PORT, limit=MAX_LINE
        )
        self.reader, self.writer = reader, writer
        await send_json(writer, {"type": "parent"})
        self.recv_task = asyncio.create_task(self.recv_loop(reader, writer))
        print(
            f"[PARENT] 已连接中转 {RELAY_HOST}:{RELAY_PORT}，"
            f"当前目标网关 {self.target}"
        )

    async def ensure_connected(self):
        """连接可用返回 True；断开则尝试重连。"""
        if self.writer is not None and not self.writer.is_closing():
            return True
        try:
            await self.connect()
            return True
        except OSError as e:
            print(f"[PARENT] 连接中转失败: {e}")
            return False

    async def on_message(self, msg):
        t = msg.get("type")
        if t == "status":
            self.online = list(msg.get("gateways") or [])
            suffix = "" if self.target in self.online else "（当前目标不在线）"
            print(f"[PARENT] 在线网关: {self.online or '无'}{suffix}")
        elif t == "event":
            ev, did = msg.get("event"), msg.get("device_id")
            if ev == "gateway_online":
                if did not in self.online:
                    self.online.append(did)
                    self.online.sort()
                print(f"[PARENT] 网关上线: {did}")
            elif ev == "gateway_offline":
                if did in self.online:
                    self.online.remove(did)
                reason = msg.get("reason")
                print(f"[PARENT] 网关下线: {did}" + (f"（{reason}）" if reason else ""))
        elif t == "result":
            req_id = msg.get("req_id")
            item = self.pending.pop(req_id, None) if req_id else None
            if item:
                item["task"].cancel()
                prefix = (
                    f"[回执 {req_id}] {item['action']} {item['target']} "
                    f"@ {msg.get('device_id')}"
                )
            else:
                prefix = f"[回执] {msg.get('device_id')}"
            p = msg.get("payload") or {}
            state = "成功" if p.get("ok") else "失败"
            print(f"{prefix}: {state} - {p.get('msg')}")
        elif t == "error":
            print(f"[PARENT] 中转返回错误: {msg.get('msg')}")

    async def recv_loop(self, reader, writer):
        try:
            while True:
                msg = await read_json(reader)
                if msg is None:
                    break
                if msg:
                    await self.on_message(msg)
        except asyncio.CancelledError:
            raise
        finally:
            if self.writer is writer:
                self.writer = None
                self.reader = None
                for item in self.pending.values():
                    item["task"].cancel()
                self.pending.clear()
                if not self.closing:
                    print("[PARENT] 与中转断开，下条命令会自动重连")

    async def _timeout(self, req_id):
        await asyncio.sleep(CMD_TIMEOUT)
        item = self.pending.pop(req_id, None)
        if item:
            print(
                f"[PARENT] {item['action']} {item['target']}（{req_id}）"
                f" 超过 {CMD_TIMEOUT:g}s 未收到回执"
            )

    async def _send(self, msg) -> bool:
        if not await self.ensure_connected():
            print("[PARENT] 未连接中转，命令未发送")
            return False
        if not await send_json(self.writer, msg):
            self.writer = None
            print("[PARENT] 发送失败，连接已断开")
            return False
        return True

    async def send_cmd(self, action, raw_target):
        kind, target = normalize_target(raw_target)
        if kind is None:
            print(f"[PARENT] {target}")
            return
        req_id = uuid.uuid4().hex[:8]
        msg = {
            "type": "cmd",
            "req_id": req_id,
            "device_id": self.target,
            "payload": {"action": action, "target": target},
        }
        # 先登记再发送，避免回执比登记更快到达
        task = asyncio.create_task(self._timeout(req_id))
        self.pending[req_id] = {"action": action, "target": target, "task": task}
        if not await self._send(msg):
            self.pending.pop(req_id, None)
            task.cancel()
            return
        print(f"[PARENT] 已发送 {action} {target} -> {self.target}（req {req_id}）")

    async def request_list(self):
        if await self._send({"type": "list"}):
            pass  # 结果由 recv_loop 打印

    async def close(self):
        self.closing = True
        for item in self.pending.values():
            item["task"].cancel()
        self.pending.clear()
        if self.recv_task is not None:
            self.recv_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self.recv_task
        await close_writer(self.writer)
        self.writer = None


async def main():
    client = ParentClient()
    print(HELP.format(target=client.target))
    await client.ensure_connected()
    loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                line = await loop.run_in_executor(None, input, "> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            line = (line or "").strip()
            if not line:
                continue
            parts = line.split()
            cmd = parts[0].lower()
            if cmd in ("quit", "exit"):
                break
            elif cmd in ("help", "?"):
                print(HELP.format(target=client.target))
            elif cmd in ("list", "status"):
                await client.request_list()
            elif cmd == "use":
                if len(parts) != 2:
                    print("用法: use <设备号>")
                else:
                    client.target = parts[1]
                    mark = "" if client.target in client.online else "（离线）"
                    print(f"[PARENT] 当前目标网关: {client.target}{mark}")
            elif cmd in ("block", "unblock"):
                if len(parts) != 2:
                    print(f"用法: {cmd} <IP|域名>")
                else:
                    await client.send_cmd(cmd, parts[1])
            else:
                print("未知命令，输入 help 查看帮助")
    finally:
        await client.close()
        print("[PARENT] 已退出")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
