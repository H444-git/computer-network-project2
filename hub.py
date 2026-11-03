"""中转核心状态：TCP 家长端与手机网页控制台共用。"""

import asyncio
import time
from collections import deque

from common import close_writer, send_json

PENDING_TTL = 120   # 未回执请求记录的保留时间（秒）
HISTORY_SIZE = 100  # 内存中保留的执行记录条数


class Hub:
    def __init__(self):
        self.gateways = {}     # device_id -> {"writer", "last_hb", "blocked", "mode"}
        self.parents = {}      # TCP 家长端 writer -> None
        self.web_clients = {}  # 网页客户端 client_id -> asyncio.Queue
        self.pending = {}      # req_id -> {"kind": "tcp"|"web", "target", "ts"}
        self.history = deque(maxlen=HISTORY_SIZE)

    # ---------- 查询 ----------

    def devices(self):
        return [
            {
                "id": did,
                "blocked": list(gw.get("blocked") or []),
                "mode": gw.get("mode") or "",
            }
            for did, gw in sorted(self.gateways.items())
        ]

    def status(self):
        return {
            "type": "status",
            "gateways": sorted(self.gateways),
            "devices": self.devices(),
            "history": list(self.history),
        }

    # ---------- 推送 ----------

    async def broadcast(self, obj, exclude_writer=None):
        """推送给所有 TCP 家长端和网页客户端。"""
        for writer in list(self.parents):
            if writer is exclude_writer:
                continue
            if not await send_json(writer, obj):
                self.parents.pop(writer, None)
                await close_writer(writer)
        self.broadcast_web(obj)

    def broadcast_web(self, obj):
        for cid, queue in list(self.web_clients.items()):
            try:
                queue.put_nowait(obj)
            except asyncio.QueueFull:
                # 客户端消费过慢，断开这个 SSE 连接，避免无限积压
                self.web_clients.pop(cid, None)

    async def event(self, event, device_id, reason=None):
        msg = {"type": "event", "event": event, "device_id": device_id}
        if reason:
            msg["reason"] = reason
        print(f"[RELAY] 事件 {event}: {device_id}" + (f"（{reason}）" if reason else ""))
        await self.broadcast(msg)

    # ---------- 指令与回执 ----------

    async def submit(self, source, req_id, device_id, action, target):
        """下发指令。source 为 ("tcp", writer) 或 ("web", client_id)。

        返回 (ok, 错误信息)。
        """
        gw = self.gateways.get(device_id)
        if gw is None:
            return False, "网关不在线"
        if req_id:
            self.pending[req_id] = {
                "kind": source[0],
                "target": source[1],
                "ts": time.time(),
            }
        out = {
            "type": "cmd",
            "req_id": req_id,
            "device_id": device_id,
            "payload": {"action": action, "target": target},
        }
        if await send_json(gw["writer"], out):
            return True, None
        # 网关连接实际已断开：清理并通知
        self.pending.pop(req_id, None)
        self.gateways.pop(device_id, None)
        await close_writer(gw["writer"])
        await self.event("gateway_offline", device_id, "send_failed")
        return False, "网关连接异常"

    def record(self, msg):
        payload = msg.get("payload") or {}
        self.history.append(
            {
                "time": time.strftime("%H:%M:%S"),
                "device_id": msg.get("device_id"),
                "action": payload.get("action"),
                "target": payload.get("target"),
                "ok": bool(payload.get("ok")),
                "msg": payload.get("msg"),
            }
        )

    async def deliver_result(self, msg):
        """把网关回执路由回发起者（TCP 家长端或网页客户端）。"""
        req_id = msg.get("req_id")
        entry = self.pending.pop(req_id, None) if req_id else None
        self.record(msg)
        if entry is None:
            # 没有对应请求（例如网关主动上报）时广播
            await self.broadcast(msg)
            return
        if entry["kind"] == "tcp":
            writer = entry["target"]
            if writer in self.parents:
                if not await send_json(writer, msg):
                    self.parents.pop(writer, None)
                    await close_writer(writer)
            # 发起者已断线：丢弃，避免回执串给其他家长端
        else:
            queue = self.web_clients.get(entry["target"])
            if queue is not None:
                try:
                    queue.put_nowait(msg)
                except asyncio.QueueFull:
                    self.web_clients.pop(entry["target"], None)
            else:
                self.broadcast_web(msg)

    def prune_pending(self):
        t = time.time()
        for req_id, entry in list(self.pending.items()):
            if t - entry["ts"] > PENDING_TTL:
                self.pending.pop(req_id, None)
