"""家庭网关端：接收中转下发的阻断/解除指令，执行并回报结果。

支持 IP 与域名：域名会解析成 A/AAAA 地址后逐条应用规则。
"""

import asyncio
import os
import subprocess
import time
from contextlib import suppress

from common import (
    DEVICE_ID,
    HEARTBEAT_INTERVAL,
    LOG_FILE,
    MAX_LINE,
    RELAY_HOST,
    RELAY_PORT,
    close_writer,
    normalize_ip,
    normalize_target,
    read_json,
    send_json,
)

# 有 root 且 Linux 时设置环境变量 USE_REAL_IPTABLES=1 启用真实规则
USE_REAL_IPTABLES = os.environ.get("USE_REAL_IPTABLES", "0") == "1"

# 模拟模式下已阻断的 IP 集合
blocked_ips = set()

# 管控目标（保留家长输入的原始名字）-> 该目标解析出的 IP 列表
blocked_targets = {}

# 连接持续超过这个时长才认为稳定，下次断线时把退避重置回 1 秒
STABLE_SECONDS = 30


def log(line):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {line}\n")


def _iptables_base(ip):
    # IPv6 使用 ip6tables；-w 等待 xtables 锁，避免并发调用报错
    return ["ip6tables" if ":" in ip else "iptables", "-w"]


def _apply_iptables(action, ip):
    """真实模式：先用 -C 检查规则是否存在，保证幂等。"""
    base = _iptables_base(ip)
    rule = ["OUTPUT", "-d", ip, "-j", "DROP"]
    try:
        exists = subprocess.run(
            base + ["-C"] + rule, capture_output=True, text=True
        ).returncode == 0
        if action == "block":
            if exists:
                return True, f"{ip} 已处于阻断状态（iptables 规则已存在）"
            r = subprocess.run(base + ["-A"] + rule, capture_output=True, text=True)
            if r.returncode == 0:
                return True, f"iptables 已阻断 {ip}"
            return False, f"iptables 添加失败: {r.stderr.strip()}"
        else:
            if not exists:
                return True, f"{ip} 未处于阻断状态（无需解除）"
            r = subprocess.run(base + ["-D"] + rule, capture_output=True, text=True)
            if r.returncode == 0:
                return True, f"iptables 已解除 {ip}"
            return False, f"iptables 删除失败: {r.stderr.strip()}"
    except FileNotFoundError:
        return False, "未找到 iptables 命令（当前系统不支持）"
    except Exception as e:
        return False, f"iptables 执行异常: {e}"


def apply_rule(action, ip):
    """对单个 IP 执行一条规则，返回 (ok, 描述)。"""
    if action not in ("block", "unblock"):
        return False, f"未知指令: {action}"
    if USE_REAL_IPTABLES:
        return _apply_iptables(action, ip)
    if action == "block":
        if ip in blocked_ips:
            return True, f"[模拟] {ip} 已处于阻断状态"
        blocked_ips.add(ip)
        return True, f"[模拟] 已阻断 {ip}"
    else:
        if ip not in blocked_ips:
            return True, f"[模拟] {ip} 未处于阻断状态（无需解除）"
        blocked_ips.discard(ip)
        return True, f"[模拟] 已解除 {ip}"


async def resolve_target(target):
    """把管控目标解析成 IP 列表，返回 (ips, 错误信息)。"""
    ip = normalize_ip(target)
    if ip is not None:
        return [ip], None

    kind, value = normalize_target(target)
    if kind != "host":
        return None, value  # value 是错误信息

    try:
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(value, None), 5
        )
    except asyncio.TimeoutError:
        return None, f"域名 {value} 解析超时"
    except OSError as e:
        return None, f"域名 {value} 解析失败: {e}"

    ips = []
    for info in infos:
        addr = info[4][0]
        if addr not in ips:
            ips.append(addr)
    if not ips:
        return None, f"域名 {value} 没有解析到可用地址"
    return ips, None


async def do_rule(action, target):
    """执行一条管控指令，返回 (ok, 描述)。"""
    if action not in ("block", "unblock"):
        return False, f"未知指令: {action}"
    prefix = "" if USE_REAL_IPTABLES else "[模拟] "

    if action == "block":
        if target in blocked_targets:
            return True, f"{prefix}{target} 已处于阻断状态"
        ips, err = await resolve_target(target)
        if err:
            return False, err
        applied = []
        for ip in ips:
            ok, msg = apply_rule("block", ip)
            if not ok:
                # 部分失败时回滚，避免状态不一致
                for done in applied:
                    apply_rule("unblock", done)
                return False, msg
            applied.append(ip)
        blocked_targets[target] = applied
        extra = ""
        if target != applied[0]:
            shown = ", ".join(applied[:3])
            extra = f"（解析到 {shown}{' 等' if len(applied) > 3 else ''}）"
        return True, f"{prefix}已阻断 {target}{extra}"

    # unblock
    was_blocked = target in blocked_targets
    if was_blocked:
        ips = blocked_targets.pop(target)
    else:
        ips, err = await resolve_target(target)
        if err:
            return False, err

    still_used = {ip for addrs in blocked_targets.values() for ip in addrs}
    skipped = 0
    for ip in ips:
        if ip in still_used:
            skipped += 1  # 该 IP 仍被其他管控目标引用
            continue
        ok, msg = apply_rule("unblock", ip)
        if not ok:
            return False, msg
    if not was_blocked:
        return True, f"{prefix}{target} 未处于阻断状态（无需解除）"
    note = f"（{skipped} 个 IP 仍被其他规则使用）" if skipped else ""
    return True, f"{prefix}已解除 {target}{note}"


def state_payload():
    return {
        "blocked": sorted(blocked_targets),
        "mode": "iptables" if USE_REAL_IPTABLES else "simulate",
    }


async def heartbeat(writer):
    """定时发送心跳；发送失败时关闭连接，让读循环退出并触发重连。"""
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            ok = await send_json(
                writer, {"type": "heartbeat", "device_id": DEVICE_ID}
            )
            if not ok:
                await close_writer(writer)
                return
    except asyncio.CancelledError:
        raise


async def session():
    """建立一次连接并处理消息，返回本次连接持续了多久（秒）。"""
    reader, writer = await asyncio.open_connection(
        RELAY_HOST, RELAY_PORT, limit=MAX_LINE
    )
    await send_json(writer, {"type": "register", "device_id": DEVICE_ID})
    await send_json(
        writer, {"type": "state", "device_id": DEVICE_ID, "payload": state_payload()}
    )
    print("[GW] 已注册到中转")
    log("注册到中转")
    started = time.monotonic()
    hb = asyncio.create_task(heartbeat(writer))
    try:
        while True:
            msg = await read_json(reader)
            if msg is None:
                print("[GW] 中转断开")
                break
            if msg.get("type") != "cmd":
                continue
            req_id = msg.get("req_id")
            payload = msg.get("payload") or {}
            action = payload.get("action")
            target = str(payload.get("target") or "").strip()
            print(f"[GW] 收到指令 {action} {target}")

            ok, result = await do_rule(action, target)
            print(f"[GW] {result}")
            log(
                f"req={req_id} {action} {target} -> "
                f"[{'OK' if ok else 'FAIL'}] {result}"
            )

            if not await send_json(
                writer,
                {
                    "type": "result",
                    "req_id": req_id,
                    "device_id": DEVICE_ID,
                    "payload": {
                        "ok": ok,
                        "msg": result,
                        "action": action,
                        "target": target,
                    },
                },
            ):
                break
            if not await send_json(
                writer,
                {
                    "type": "state",
                    "device_id": DEVICE_ID,
                    "payload": state_payload(),
                },
            ):
                break
    finally:
        hb.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await hb
        await close_writer(writer)
    return time.monotonic() - started


async def main():
    delay = 1
    print(f"[GW] 设备 {DEVICE_ID}，目标中转 {RELAY_HOST}:{RELAY_PORT}")
    while True:
        try:
            lived = await session()
            if lived >= STABLE_SECONDS:
                delay = 1  # 稳定运行过，下次重连从最短退避开始
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[GW] 连接失败: {e}")
            log(f"连接中转失败: {e}")
        print(f"[GW] {delay}s 后重连...")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[GW] 已退出")
