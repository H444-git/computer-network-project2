"""relay / gateway / parent 共用的配置与工具函数。"""

import ipaddress
import json
import os
import re
import socket
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "gateway_actions.log"

# 连接参数，可用环境变量覆盖（例如把中转部署到另一台机器时设置 RELAY_HOST）
RELAY_HOST = os.environ.get("RELAY_HOST") or "127.0.0.1"
RELAY_BIND = os.environ.get("RELAY_BIND") or "0.0.0.0"
RELAY_PORT = int(os.environ.get("RELAY_PORT") or 9000)

# 设备标识
DEVICE_ID = os.environ.get("DEVICE_ID") or "gw1"
TARGET_DEVICE = os.environ.get("TARGET_DEVICE") or "gw1"

# 心跳与超时（秒）。测试脚本会调小这些值来加快验证。
HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL") or 10)
HEARTBEAT_TIMEOUT = float(os.environ.get("HEARTBEAT_TIMEOUT") or 30)
CMD_TIMEOUT = float(os.environ.get("CMD_TIMEOUT") or 15)

# 手机网页控制台
WEB_BIND = os.environ.get("WEB_BIND") or "0.0.0.0"
WEB_PORT = int(os.environ.get("WEB_PORT") or 8080)
WEB_TOKEN = os.environ.get("WEB_TOKEN") or "parent123"
WEB_DIR = BASE_DIR / "web"

# 单条 JSON 消息的最大长度，防止超大报文撑爆内存
MAX_LINE = 64 * 1024

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"[a-zA-Z]{2,63}$"
)


def now() -> float:
    return time.time()


def normalize_ip(text):
    """校验并规范化 IP 地址；非法时返回 None。支持 IPv4 / IPv6。"""
    try:
        return str(ipaddress.ip_address(str(text).strip()))
    except ValueError:
        return None


def normalize_target(text):
    """校验管控目标（IP 或域名）。

    返回 (kind, value)：kind 为 "ip" / "host"；非法时返回 (None, 错误信息)。
    """
    raw = str(text or "").strip()
    ip = normalize_ip(raw)
    if ip is not None:
        return "ip", ip
    host = raw.rstrip(".").lower()
    if _DOMAIN_RE.match(host):
        return "host", host
    return None, "只支持 IP 地址或域名（例如 1.2.3.4 或 douyin.com）"


def local_ips():
    """枚举本机局域网 IPv4 地址，用于提示手机访问地址。"""
    ips = []
    try:
        for addr in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not addr.startswith("127.") and addr not in ips:
                ips.append(addr)
    except OSError:
        pass
    return ips


async def read_json(reader):
    """读取一行 JSON。

    返回解析后的 dict；连接断开返回 None；非法消息返回空 dict（调用方忽略即可）。
    """
    try:
        line = await reader.readline()
    except (ValueError, ConnectionError, OSError):
        # ValueError: 单行超过 limit；ConnectionError: 对端复位
        return None
    if not line:
        return None
    try:
        msg = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return msg if isinstance(msg, dict) else {}


async def send_json(writer, obj) -> bool:
    """发送一条 JSON 行，成功返回 True，连接已损坏返回 False。"""
    if writer is None or writer.is_closing():
        return False
    try:
        writer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()
        return True
    except (ConnectionError, OSError, RuntimeError):
        return False


async def close_writer(writer):
    """安静地关闭连接，重复调用不会报错。"""
    if writer is None:
        return
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass
