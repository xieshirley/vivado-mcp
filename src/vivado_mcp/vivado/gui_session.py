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
    port: int,
    timeout: float = _CURPROJ_TIMEOUT,
) -> str | None:
    """一次性独立短连接查询 current_project(PRD A2 横幅提示用)。

    **绝不走会话主连接**:主连接上的查询一旦超时,迟到的响应会残留在流上,
    让后续所有 execute 的请求/响应永久错位一格(0.3.22 审计 P1,fake server
    实测复现)。本函数与 :func:`probe_vmcp_server` 同款同步收发:
    连接 → 发一帧 QUERY_CURRENT_PROJECT → 读一帧 → 关闭,失败即丢弃连接。

    Returns:
        current_project 名称(无项目时为 ``""``);任何失败返回 None 并
        logger.warning 具体原因 —— 失败只意味着 banner 无提示。
    """
    payload = QUERY_CURRENT_PROJECT.encode("utf-8")
    header = len(payload).to_bytes(4, "big")
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(header + payload)
            hdr = _recv_exact(s, 4)
            if hdr is None:
                logger.warning(
                    "current_project 查询读响应头失败(对端关闭或 %ss 超时),"
                    "banner 省略项目提示",
                    timeout,
                )
                return None
            resp_len = int.from_bytes(hdr, "big")
            if resp_len <= 0 or resp_len > _HANDSHAKE_MAX_RESP:
                logger.warning(
                    "current_project 查询响应长度非法: %d,banner 省略项目提示",
                    resp_len,
                )
                return None
            body = _recv_exact(s, resp_len)
            if body is None:
                logger.warning(
                    "current_project 查询读响应体失败(对端关闭或 %ss 超时),"
                    "banner 省略项目提示",
                    timeout,
                )
                return None
            obj = json.loads(body.decode("utf-8"))
            output = str(obj.get("output", ""))
    except (OSError, socket.timeout) as e:
        logger.warning(
            "current_project 查询连接/收发失败(banner 省略项目提示): %s", e
        )
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(
            "current_project 查询响应解析失败(banner 省略项目提示): %s", e
        )
        return None

    for line in output.splitlines():
        if line.startswith("VMCP_CURPROJ:"):
            return line[len("VMCP_CURPROJ:") :].strip()
    logger.warning(
        "current_project 查询输出缺 VMCP_CURPROJ 标记,前 200 字: %r",
        output[:200],
    )
    return None


class GuiSession(BaseSession):
    """连接到 Vivado GUI 的 TCP 会话。"""

    def __init__(
        self,
        vivado_path: str,
        session_id: str = "default",
        port: int = 0,
        attach_only: bool = False,
    ):
        super().__init__(vivado_path=vivado_path, session_id=session_id)
        # 端口意图哨兵(B 方案):
        #   port == 0 = 未指定 → auto-alloc 一个空闲端口 spawn 全新独立实例,**跳过 probe**
        #               (多开正解:连开两次 = 两个独立 GUI,不会被 probe 抢成 attach)
        #   port  > 0 = 显式目标端口 → 先 probe→命中则 attach,否则 spawn 并绑该确切端口
        # attach 模式下 port 始终是要连的显式端口(attach 本就需知道连哪)。
        self._port_preference = port
        self._attach_only = attach_only
        # probe-then-attach 命中外部 GUI(用户手动启动 + init.tcl 已注入)时为 True
        # 与 _attach_only 区别:_attach_only 是用户显式请求,_attached_external
        # 是 mode="gui" 时的隐式 attach。两者对 mode/stop 行为意义相同。
        self._attached_external: bool = False
        self._proc: asyncio.subprocess.Process | None = None
        # auto-alloc 出来的确切端口(port==0 路径);显式端口路径下保持 None。
        self._allocated_port: int | None = None
        # spawn 的 vivado pid,供 stop() 在 self._proc 引用丢失时按 pid 精杀。
        # attach / 命中外部 GUI 路径本就不 spawn,_pid 保持 None,被 stop 双守卫挡住。
        self._pid: int | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected_port: int | None = None
        self._lock = asyncio.Lock()
        self._closing = False
        # 调用方超时不能取消底层读帧；否则迟到响应会被下一条命令误收。
        self._inflight_task: asyncio.Task[TclResult] | None = None
        self._response_phase: str | None = None
        self._tmp_script: str | None = None
        # 上一条 execute 超时且响应尚未收到时为 True(下次成功收到一帧即清除)。
        # list_sessions 探活守卫据此跳过 fresh probe:Vivado 单线程 event loop
        # 正在跑已超时的长命令时,新连接得不到服务,1s 探活必然落空 ≠ 挂死。
        self._pending_response: bool = False
        # P0 fix: stderr ring buffer + drain task (issue #6)
        self._stderr_buffer: collections.deque[str] = collections.deque(maxlen=200)
        self._stderr_task: asyncio.Task | None = None
        # P2 fix: stdout ring buffer + drain task (issue #6)
        self._stdout_buffer: collections.deque[str] = collections.deque(maxlen=200)
        self._stdout_task: asyncio.Task | None = None

    @property
    def mode(self) -> str:
        # 实际行为而非用户请求:外部 attach(显式 attach_only 或 probe 命中)
        # 都报 "attach",让上层(AI / list_sessions)能直接看出命令落到哪
        if self._attach_only or self._attached_external:
            return "attach"
        return "gui"

    @property
    def connected_port(self) -> int | None:
        """已连接的 TCP 端口(尚未连接为 None)。"""
        return self._connected_port

    @property
    def attached_external(self) -> bool:
        """是否 attach 到了非 MCP spawn 的 Vivado(用户手动启动 + init.tcl)。"""
        return self._attached_external

    @property
    def pid(self) -> int | None:
        """本 session spawn 的 vivado 进程 pid(attach / 外部命中路径为 None)。"""
        return self._pid

    @property
    def probe_port(self) -> int:
        """TCP server 期望的端口号(probe / attach 路径用)。"""
        return self._connected_port or self._port_preference or 9999

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _handshake(self, reader, writer) -> bool:
        """握手：发 ``puts VMCP_HANDSHAKE_ACK``,验响应 output 含 ACK + valid JSON。

        仅握手完成才能进入 execute 主循环。失败时关连接抛错。
        """
        import json as _json
        import struct as _struct

        try:
            ack_token = "VMCP_HANDSHAKE_ACK_" + uuid.uuid4().hex[:8]
            payload = f"puts {ack_token}".encode("utf-8")
            writer.write(len(payload).to_bytes(4, "big") + payload)
            await writer.drain()
            hdr = await reader.readexactly(4)
            resp_len = _struct.unpack(">I", hdr)[0]
            if resp_len <= 0 or resp_len > _HANDSHAKE_MAX_RESP:
                return False
            body = await reader.readexactly(resp_len)
            obj = _json.loads(body.decode("utf-8"))
            if not (isinstance(obj, dict) and "rc" in obj and "output" in obj):
                return False
            return ack_token in str(obj.get("output", ""))
        except Exception:
            return False

    async def start(self, timeout: float = 120.0) -> str:
        """启动 GUI 会话。

        ``attach_only=False``:通过 cmd.exe 包装启动 ``vivado.bat``(loader.bat 会基于
        ``PROCESSOR_ARCHITECTURE`` 自动检测 OS arch)。加载完成后 probe TCP server
        等待握手,失败时调用 :meth:`_cleanup_failed_spawn` 避免孤儿进程。

        ``attach_only=True``:假设外部 GUI 已启动且 init.tcl 注入了 TCP server,
        MCP 直接连 port=``probe_port`` 即可。失败时不清理外部进程。

        Returns:
            启动横幅(版本 + current_project 提示)。
        """
        if self.alive:
            return f"会话 '{self.session_id}' 已在运行中(mcp_is_alive=True)。"

        self._closing = False
        self._state = SessionState.STARTING
        logger.info("启动 GUI 会话 '%s': %s", self.session_id, self.vivado_path)

        if not self._attach_only:
            # Determine target port and spawn vivado GUI
            if self._port_preference > 0:
                target_port = self._port_preference
            else:
                self._allocated_port = self._alloc_free_port()
                target_port = self._allocated_port
                logger.info(
                    "会话 '%s' 未指定端口,auto-alloc 空闲端口 %d 启动独立实例",
                    self.session_id,
                    target_port,
                )

            try:
                script_path = _locate_server_script()
            except FileNotFoundError as e:
                self._state = SessionState.ERROR
                raise RuntimeError(str(e)) from e

            try:
                # 关键：-source 临时注入 tcl server（即使用户没跑 install 也能工作）
                # 注入 VMCP_PORT_PREF = 确切端口,tcl server 绑这个端口否则退出
                # (不再池滑动 → 杜绝新 vivado 监听在没人连的端口=孤儿)
                import tempfile
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".tcl", delete=False, encoding="utf-8"
                ) as tmp:
                    tmp.write(f"set ::VMCP_PORT_PREF {target_port}\n")
                    tmp.write(f'source "{script_path.as_posix()}"\n')
                    tmp_script = tmp.name
                self._tmp_script = tmp_script
                # atexit 兜底:MCP 进程被强杀时仍会清理
                _TMP_SCRIPTS.add(tmp_script)

                # D 方案(issue #6 P1 增强):Python 进程(包括 32-bit Python)收不到
                # Windows 系统动态变量 PROCESSOR_ARCHITECTURE(实测:即使显式
                # env={PROCESSOR_ARCHITECTURE: AMD64} 传给子进程,子 .bat 里
                # %PROCESSOR_ARCHITECTURE% 仍是空)。但 cmd.exe 进程能读到。
                # 改用 cmd.exe /c vivado.bat 让 cmd 解释器传递 PROC_ARCH,
                # loader.bat 检测到 64-bit 走 win64 分支。实测有效。
                self._proc = await asyncio.create_subprocess_exec(
                    "cmd.exe", "/c", self.vivado_path,
                    "-mode", "gui",
                    "-source", tmp_script,
                    "-nojournal", "-nolog",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                # B5 + P2 fix: start stderr + stdout drain tasks
                self._stderr_task = asyncio.create_task(self._drain_stderr())
                self._stdout_task = asyncio.create_task(self._drain_stdout())
                # 记下 pid:stop() 在 self._proc 引用丢失时仍能按 pid 精杀
                self._pid = self._proc.pid
                logger.info(
                    "已 spawn Vivado GUI (pid=%s, 目标端口=%d), 等待 TCP server 就绪...",
                    self._proc.pid,
                    target_port,
                )
            except (OSError, FileNotFoundError) as e:
                self._state = SessionState.ERROR
                raise RuntimeError(f"启动 Vivado GUI 失败: {e}") from e

        # ---- 2. 只连那一个确切端口,轮询直到新 vivado 起完 ----
        # 不再扫端口池:连别人的端口正是 0.3.19 串台的根因。
        #   spawn 路径 → 连 auto-alloc / 显式注入的确切端口
        #   attach 路径 → 连用户给的显式端口
        target_port = (
            self._allocated_port
            if self._allocated_port is not None
            else self._port_preference
        )

        # spawn 中的目标端口登记进 pending 集合:此时 session 尚未进
        # SessionManager._sessions,list_sessions 外部探测会把自己正在启动的
        # GUI 误报为 external,靠这个集合跳过。try/finally 保证异常路径也移除。
        spawned = self._proc is not None
        if spawned:
            _PENDING_SPAWN_PORTS.add(target_port)

        try:
            deadline = time.time() + timeout
            connect_err: Exception | None = None
            while time.time() < deadline:
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection("127.0.0.1", target_port),
                        timeout=2.0,
                    )
                except (ConnectionRefusedError, asyncio.TimeoutError, OSError) as e:
                    # 新 vivado 还没起完 → 等下一轮重试同一个确切端口
                    connect_err = e
                else:
                    # 连上后必须握手验证:确认对面说的是我们的 length-prefix 协议
                    # (避免连到 SynthPilot 等其他产品的 server 上)
                    handshake_ok = await self._handshake(reader, writer)
                    if handshake_ok:
                        self._reader = reader
                        self._writer = writer
                        self._connected_port = target_port
                        self._state = SessionState.READY
                        self._start_time = time.time()
                        msg = (
                            f"GUI 会话就绪:attach={self._attach_only},"
                            f" 端口 {target_port}"
                        )
                        logger.info(msg)
                        return msg + await self._current_project_hint()
                    logger.debug(
                        "端口 %d 握手失败(可能是其他产品的 server),重试",
                        target_port,
                    )
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass

                # 进程还活吗
                if self._proc is not None and self._proc.returncode is not None:
                    self._state = SessionState.ERROR
                    stderr_tail = self._recent_stderr(max_lines=30)
                    stdout_tail = self._recent_stdout(max_lines=30)
                    parts = []
                    if stderr_tail:
                        parts.append(f"\n--- stderr (recent 30 lines) ---\n{stderr_tail}")
                    if stdout_tail:
                        parts.append(f"\n--- stdout (recent 30 lines) ---\n{stdout_tail}")
                    raise RuntimeError(
                        f"Vivado GUI 进程提前退出 "
                        f"(returncode={self._proc.returncode})"
                        + "".join(parts)
                    )
                await asyncio.sleep(2.0)

            # 超时:先杀掉自己 spawn 的 Vivado 再抛 —— 异常上传后 session 不会
            # 进 SessionManager._sessions,stop_session 无从清理,不杀就是真孤儿
            await self._cleanup_failed_spawn()
            self._state = SessionState.ERROR
            stderr_tail = self._recent_stderr(max_lines=30)
            stdout_tail = self._recent_stdout(max_lines=30)
            parts = []
            if stderr_tail:
                parts.append(f"\n--- stderr (recent 30 lines) ---\n{stderr_tail}")
            if stdout_tail:
                parts.append(f"\n--- stdout (recent 30 lines) ---\n{stdout_tail}")
            raise RuntimeError(
                f"连接 Vivado GUI 超时({timeout}s,确切端口 {target_port})。"
                f"该端口可能被其他进程抢占,请重试。最后一次错误: {connect_err}"
                + "".join(parts)
            )
        finally:
            if spawned:
                _PENDING_SPAWN_PORTS.discard(target_port)

    async def _cleanup_failed_spawn(self) -> None:
        """start() 超时/失败时杀掉自己 spawn 的 Vivado,防止产生孤儿进程。

        start 失败异常上传后 session 不会进 SessionManager._sessions,
        stop_session 此后无从清理 —— 必须在抛错前自己收尾。
        attach / 外部命中路径不 spawn(_proc is None),天然跳过。
        """
        import subprocess
        import sys

        # B5 + P2 fix: cancel stderr + stdout drain tasks first
        if self._stderr_task is not None and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stderr_task = None
        if self._stdout_task is not None and not self._stdout_task.done():
            self._stdout_task.cancel()
            try:
                await self._stdout_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stdout_task = None

        if self._proc is None or self._proc.returncode is not None:
            return
        kill_pid = self._pid if self._pid is not None else self._proc.pid
        logger.warning(
            "会话 '%s' 启动失败,清理已 spawn 的 Vivado 进程 (pid=%s)",
            self.session_id,
            kill_pid,
        )
        try:
            if sys.platform == "win32":
                # /T 递归杀进程树:vivado.bat 的 cmd.exe 外壳下还有 vivado.exe
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(kill_pid)],
                    capture_output=True,
                    timeout=10.0,
                )
            else:
                # Unix: 单进程 kill(非 killpg)。vivado 是 shell 包装脚本且
                # 子进程未被 exec 接管时可能留孤儿 —— 项目主战场是 Windows
                # 真机,暂不做 start_new_session+killpg 改造(与 stop() 步骤 3 同)
                self._proc.kill()
            await asyncio.wait_for(self._proc.wait(), timeout=10.0)
        except Exception as e:
            logger.warning(
                "清理 spawn 失败的 Vivado (pid=%s) 异常: %s", kill_pid, e
            )

    async def _current_project_hint(self) -> str:
        """启动横幅的项目状态提示(PRD A2)。

        spawn 出来的全新 GUI / 用户停在 Start Page 的 GUI 没打开任何项目
        (current_project 为空或 "New Project"),AI 直接跑 report_* 只会
        拿到一串错。此时提示先 open_project。

        查询走 :func:`_query_current_project` 的**一次性独立短连接**,主连接
        零接触 —— 在主连接上 execute 查询一旦超时,迟到的响应会让后续所有命令
        的结果永久错位(0.3.22 审计 P1)。查询失败不阻塞启动,降级为无提示
        (helper 内已 log 具体原因)。同步收发包进 to_thread,不阻塞 event loop。
        """
        if self._connected_port is None:
            return ""
        proj = await asyncio.to_thread(
            _query_current_project,
            "127.0.0.1",
            self._connected_port,
        )
        if not proj:
            return ""
        return f"\n提示: 当前 project={proj},如非预期请先 close_project -quiet 再 open_project <绝对路径>"

    async def _drain_stderr(self) -> None:
        """后台任务:持续读取 Vivado stderr,存入环形缓冲区(issue #6 P0)。"""
        assert self._proc and self._proc.stderr
        try:
            while True:
                raw = await self._proc.stderr.readline()
                if not raw:
                    break
                line = decode_vivado_output(raw).rstrip("\r\n")
                if line:
                    self._stderr_buffer.append(line)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("[%s] stderr drain exception: %s", self.session_id, e)

    async def _drain_stdout(self) -> None:
        """后台任务:持续读取 Vivado stdout,存入环形缓冲区(issue #6 P2)。

        Vivado loader.bat 失败诊断信息(如 'Could not find 32-bit executable')
        走 stdout,不用 PIPE 会完全黑盒。正常 GUI 启动后 Vivado 极少写 stdout,
        200 行 buffer 足够覆盖失败诊断。
        """
        assert self._proc and self._proc.stdout
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break
                line = decode_vivado_output(raw).rstrip("\r\n")
                if line:
                    self._stdout_buffer.append(line)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("[%s] stdout drain exception: %s", self.session_id, e)

    def _recent_stderr(self, max_lines=30):
        """返回 stderr 缓冲区最近 N 行,用于失败时附加诊断。
        """
        lines = list(self._stderr_buffer)[-max_lines:]
        return "\n".join(lines)

    def _recent_stdout(self, max_lines=30):
        """返回 stdout 缓冲区最近 N 行(issue #6 P2,loader.bat 失败诊断走 stdout)。
        """
        lines = list(self._stdout_buffer)[-max_lines:]
        return "\n".join(lines)

    async def stop(self, timeout: float = 10.0) -> None:
        """关闭 TCP 连接 + 终止 spawn 的 GUI 进程（attach 模式不终止外部进程）。

        B13 修复:原 ``_proc.terminate()`` 只杀 ``vivado.bat`` 的 cmd.exe 外壳,
        Windows 没有进程组概念,子进程 vivado.exe 会变成孤儿继续占 800MB+ 内存,
        且 Vivado自己写的 ``vivado_pid<PID>.str`` 文件不被清理。

        新策略:
        1. 先通过 TCP 发 Tcl ``exit`` 让 Vivado 优雅退出(会自动清 pid 文件)
        2. 若超时,Windows 用 ``taskkill /F /T`` 递归杀进程树,Unix 用 SIGKILL
        3. 兜底扫工作目录 ``vivado_pid*.str`` 强删
        """
        import glob as glob_mod
        import os
        import subprocess
        import sys

        self._closing = True
        self._state = SessionState.STOPPING
        logger.info("正在关闭 GUI 会话 '%s'...", self.session_id)

        # B5 + P2 fix: cancel stderr + stdout drain tasks (issue #6 P2)
        if self._stderr_task is not None and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stderr_task = None
        if self._stdout_task is not None and not self._stdout_task.done():
            self._stdout_task.cancel()
            try:
                await self._stdout_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stdout_task = None

        # 步骤 1:尝试优雅退出 —— 发 Tcl `exit`,Vivado自己清 pid/journal
        # attach 模式 OR probe-then-attach 命中外部 GUI 时,都是用户的 Vivado,不主动 exit
        inflight = self._inflight_task
        if inflight is None or inflight.done():
            # 没有在途 reader 时才获取 execute 的锁并执行裸协议退出；活跃命令
            # 场景直接关连接，避免等待长命令导致 stop 死锁。
            async with self._lock:
                if (
                    not self._attach_only
                    and not self._attached_external
                    and self._writer is not None
                ):
                    try:
                        payload = b"exit"
                        header = len(payload).to_bytes(4, "big")
                        self._writer.write(header + payload)
                        await self._writer.drain()
                        await asyncio.wait_for(
                            self._reader.read(4) if self._reader else asyncio.sleep(0),
                            timeout=5.0,
                        )
                    except Exception as e:
                        logger.debug("优雅 exit 失败(将走强杀): %s", e)

        # 步骤 2:关 socket
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception as e:
                logger.debug("关闭 writer 异常: %s", e)
            self._writer = None
            self._reader = None

        # 步骤 3:确保进程真退出。Windows 用 taskkill /T 递归杀树
        # 外部 attach(显式 attach_only 或 probe 命中)不杀进程
        if (
            self._proc is not None
            and not self._attach_only
            and not self._attached_external
        ):
            if self._proc.returncode is None:
                # taskkill 用记录的 self._pid(spawn 时存),即使 self._proc 引用因故
                # 丢失也能按确切 pid 精杀;正常情况 self._pid == self._proc.pid。
                kill_pid = self._pid if self._pid is not None else self._proc.pid
                # 先给 Vivado 一点时间自己退(响应 Tcl exit)
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    # 没退,强杀
                    try:
                        if sys.platform == "win32":
                            # 关键:/T 递归杀进程树,捕获 cmd.exe(vivado.bat)下的 vivado.exe
                            subprocess.run(
                                ["taskkill", "/F", "/T", "/PID", str(kill_pid)],
                                capture_output=True,
                                timeout=timeout,
                            )
                        else:
                            # Unix: 单进程 kill(非 killpg,旧注释"kill 进程组"
                            # 与实现不符已订正)。vivado 包装脚本场景可能留孤儿,
                            # 主战场 Windows,暂不做 killpg 改造
                            self._proc.kill()
                        await asyncio.wait_for(self._proc.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Vivado 进程 PID=%s 未在 %ss 内退出,可能成为孤儿进程",
                            kill_pid,
                            timeout,
                        )
                    except Exception as e:
                        logger.warning("强杀 Vivado 进程异常: %s", e)
            self._proc = None

        # 步骤 4:兜底清理 vivado_pid*.str(Vivado 强杀时不会自己删)
        for pid_file in glob_mod.glob("vivado_pid*.str"):
            try:
                os.remove(pid_file)
                logger.debug("已清理 %s", pid_file)
            except OSError as e:
                logger.debug("清理 %s 失败: %s", pid_file, e)

        # 步骤 5:清理临时脚本(正常路径,同时从 atexit 集合移除)
        if self._tmp_script:
            try:
                os.unlink(self._tmp_script)
            except OSError:
                pass
            _TMP_SCRIPTS.discard(self._tmp_script)
            self._tmp_script = None

        self._state = SessionState.STOPPED
        inflight = self._inflight_task
        if inflight is not None:
            if not inflight.done():
                inflight.cancel()
            try:
                await inflight
            except (asyncio.CancelledError, Exception):
                pass
            if self._inflight_task is inflight:
                self._inflight_task = None
        self._pending_response = False
        self._response_phase = None
        self._closing = False
        logger.info("GUI 会话 '%s' 已关闭。", self.session_id)
