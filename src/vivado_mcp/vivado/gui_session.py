"""GuiSession：连接到 Vivado GUI 的 TCP 会话。

两种启动方式：
1. ``attach_only=False`` （默认）—— MCP自己 spawn ``vivado -mode gui``，
   GUI 启动时 source 注入脚本开启 TCP server，然后 MCP 连上。用户**会看到 Vivado 图标**。
2. ``attach_only=True`` —— 假设用户已手动打开 Vivado（需先 ``vivado-mcp install``
   让 init.tcl 自动开 server），MCP 直接 TCP 连。

协议：length-prefix framing（4 字节 big-endian + UTF-8 payload）
- 请求 payload = Tcl 命令文本
- 响应 payload = JSON: ``{"rc": int, "output": string}``

Patches applied (see PATCH_NOTES.md for details):
- P0 (stderr): ``stderr`` 改 PIPE + 新增 ``_drain_stderr`` 后台任务，失败时通过 ``_recent_stderr``
  把 stderr 最近 N 行附加到 RuntimeError 异常。原本 ``stderr=DEVNULL`` 把所有
  启动失败信息吞了。
- P1 (loader.bat)：launcher 不再只 spawn ``vivado.bat``，而是先经过 ``cmd.exe /c``
  启动，确保 cmd 解释器能传递环境变量；同时修了上游 issue #6 中
  ``PROCESSOR_ARCHITECTURE`` 注入无效的已知问题（详见 PATCH_NOTES.md）。
- P2 (stdout)：``stdout`` 同样改 PIPE + 新增 ``_drain_stdout`` 任务，失败时通过
  ``_recent_stdout`` 把 stdout 最近 N 行附加到异常。Vivado loader.bat 的
  "Could not find 32-bit executable" 等诊断走 stdout，没有 PIPE 会完全黑盒。
"""

from __future__ import annotations

import asyncio
import atexit
import collections
import importlib.resources
import json
import logging
import os
import socket
import time
import uuid
from pathlib import Path

from vivado_mcp.tcl_script import QUERY_CURRENT_PROJECT
from vivado_mcp.vivado.base_session import BaseSession, SessionState
from vivado_mcp.vivado.tcl_utils import TclResult, clean_output, decode_vivado_output

logger = logging.getLogger(__name__)

# 默认最大响应大小（10MB）
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024

# 握手响应合理上限(超过这个值 = 端口上是别的协议,把 ASCII 当 length 解释)
_HANDSHAKE_MAX_RESP = 8192

# 进程退出兜底:记录所有临时 tcl 脚本,强杀 MCP 时也会被 atexit 清掉
# 避免 /tmp/tmp*.tcl 堆积。正常路径 stop() 会主动 unlink 并从此集合移除。
_TMP_SCRIPTS: set[str] = set()

# 正在 spawn、尚未注册进 SessionManager._sessions 的目标端口集合。
# list_sessions 外部探测据此跳过,避免把自己正在启动的 GUI 误报为 external。
# start() 在 spawn 后登记,连接循环结束(成功/失败)即移除。
# 已知窗口(审计 P3,接受现状):普通 set 非引用计数 —— 同端口并发 spawn 时
# (AI 重试/双发),先结束的 start 在 finally discard 会摘掉后者的标记,后者
# 余下启动窗口内可能被外部探测误报为 external 一次。瞬时且无害:同端口的
# 第二个 spawn 本就注定 bind 失败,不值得为此换 Counter。
_PENDING_SPAWN_PORTS: set[int] = set()

# current_project 一次性查询(PRD A2)的独立短连接超时。模块级常量便于测试覆盖。
_CURPROJ_TIMEOUT = 5.0


def _make_probe_payload() -> tuple[str, bytes]:
    """生成一次性握手探测 payload:(magic token, 未加 length 头的 payload 字节)。

    vmcp 服务端会把 ``puts`` 的 token 反射进响应 output(captured_buf 路径),
    校验方用 :func:`_verify_probe_resp` 验反射。probe(同步)与 _handshake(异步)
    **必须共用**这一份逻辑 —— 0.3.21 token 校验只打在 probe 漏了 _handshake,
    正是 B16「逻辑 fork 改一处漏一处」的重演。
    """
    token = "VMCP_PROBE_" + uuid.uuid4().hex[:16]
    return token, f"puts {token}".encode("utf-8")


def _verify_probe_resp(obj: object, token: str) -> bool:
    """校验握手响应:必须是含 rc/output 的 dict,且 output 反射了本次 token。

    仅验"dict + rc/output 字段"挡不住 VMware vNIC 等假阳性 listener
    (0.3.21 真机实测),必须验响应**内容**包含本次探测的随机 token。
    """
    if not (isinstance(obj, dict) and "rc" in obj and "output" in obj):
        return False
    return token in str(obj.get("output", ""))


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    """同步阻塞收满 n 字节，失败返回 None。"""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except (OSError, socket.timeout):
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def probe_vmcp_server(host: str, port: int, timeout: float = 0.5) -> bool:
    """同步探测 host:port 是否在跑 vivado-mcp 的 length-prefix TCP server。

    用 ``puts VMCP_PROBE_<uuid>`` 发起握手 —— vmcp 服务端会把 token 反射到
    响应 output(走 captured_buf 路径),probe 验响应 output 含该 uuid 才算
    vmcp 兼容。无副作用、只读探测。

    0.3.21 修:加 magic token 验证。0.3.19 实测中曾观察到 VMware vNIC 虚拟
    接口 listener 在某种 Windows 多接口/firewall race 下被错判为 vmcp server
    (PID 6408 绑 192.168.159.1:10000)。仅验"响应是 dict + 有 rc/output 字段"
    挡不住此类假阳性,必须验响应**内容**包含本次探测的随机 token。

    Returns:
        True 表示成功握手且响应 output 含 magic token(对面是同协议 server);
        False 表示连不上 / 协议不匹配 / 响应 output 缺 magic。
    """
    token, payload = _make_probe_payload()
    header = len(payload).to_bytes(4, "big")
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(header + payload)
            hdr = _recv_exact(s, 4)
            if hdr is None:
                return False
            resp_len = int.from_bytes(hdr, "big")
            if resp_len <= 0 or resp_len > _HANDSHAKE_MAX_RESP:
                return False
            body = _recv_exact(s, resp_len)
            if body is None:
                return False
            obj = json.loads(body.decode("utf-8"))
            # magic token 反射校验:挡掉 echo-type / 非 vmcp 服务的假阳性
            return _verify_probe_resp(obj, token)
    except (OSError, socket.timeout, json.JSONDecodeError, UnicodeDecodeError):
        return False


def _query_current_project(
    host: str,