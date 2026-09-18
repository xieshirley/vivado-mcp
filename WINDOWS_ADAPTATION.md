# Vivado MCP 本机适配与配置记录

记录日期：2026-09-18。

本文记录从上游 `vivado-mcp` 到本机 Windows + Vivado 2018.3 环境的实际修改、配置示例和复现步骤。开源项目是 Vivado 的 MCP 接口 `vivado-mcp`；Vivado 本体是 AMD/Xilinx 软件，不属于该开源仓库。

## 1. 来源与验证范围

| 项目 | 内容 |
| --- | --- |
| 上游仓库 | https://github.com/mapleleavessssssss-wq/vivado-mcp |
| 上游基线 | v0.3.25，提交 `60b13cf03397e6d58c7e16852277b678ab8a4d9d` |
| 目标个人仓库 | https://github.com/xieshirley/vivado-mcp |
| 已记录的上游问题 | https://github.com/mapleleavessssssss-wq/vivado-mcp/issues/6 |
| 上游许可证 | Apache-2.0，以仓库 `LICENSE` 为准 |
| 历史实机结果 | GUI 会话 ready，`version -short` 返回 `2018.3`，成功打开 DSSS_RX 工程 |
| 本次检查 | 本地源码差异、解释器位数、loader 修改、配置段是否存在；没有重新启动 GUI |

本文随本机最终版 `gui_session.py` 一起提交到个人仓库。本文不把历史成功记录当成本次实机测试结果；部署时仍应核对目标提交并重新验收。

## 2. 本机环境与文件位置

| 用途 | 本机路径或版本 |
| --- | --- |
| Python | 3.13.5，64 位 AMD64，已用指针大小验证 |
| Python 可执行文件 | `D:\software\py3_13_5\systemfile\python.exe` |
| 开发源码 | `D:\mcp\vivado_mcp` |
| 当前文档工作区 | `C:\Users\Shirley\.codex\worktrees\97a6\vivado_mcp` |
| 已安装 Python 包 | `D:\software\py3_13_5\systemfile\Lib\site-packages\vivado_mcp` |
| Vivado 启动入口 | `D:\software\vivado18_3\systemfile\Vivado\2018.3\bin\vivado.bat` |
| Vivado loader | `D:\software\vivado18_3\systemfile\Vivado\2018.3\bin\loader.bat` |
| Codex 用户配置 | `C:\Users\Shirley\.codex\config.toml` |

本机 `bin\unwrapped` 下检查到 `win64.o`。Python 的 `sys.platform` 返回 `win32`，这是 Windows 平台标识，**不能用于判断 Python 是 32 位**。正确检查方式：

```powershell
& 'D:\software\py3_13_5\systemfile\python.exe' -c "import sys,struct; print(sys.version); print(struct.calcsize('P') * 8)"
```

## 3. 原始故障与证据

历史上调用 `start_session(mode="gui")` 时，只能看到：

```text
Vivado GUI 进程提前退出 (returncode=1)
```

上游 GUI 子进程启动时将标准输出和标准错误重定向到 `DEVNULL`，具体失败原因被丢弃。捕获两个输出流后，历史排查得到：

```text
WARNING: .../tps/win32/jre9.0.4 does not exist.
ERROR: Could not find 32-bit executable.
ERROR: .../bin/unwrapped/win32.o/vivado.exe does not exist
ERROR: 32-bit platform is not supported.
```

这些信息说明 loader 选择了 32 位目录，而本机安装是 64 位。原始 loader 默认设置 `RDI_OS_ARCH=32`；如果启动环境缺少其依赖的架构变量，就可能保持错误的默认值。

历史排查记录中，MCP 启动链的 `PROCESSOR_ARCHITECTURE` 缺失与此现象相关。但本次没有重新观测当时的 MCP 进程环境，不能断言变量一定由某个客户端或 Node.js 删除，也没有证据证明 Windows 存在禁止传递该变量的 CreateProcess 黑名单。旧草稿中这些推断不应作为部署依据。

## 4. 实际修改

### 4.1 GUI 会话实现

修改文件：`src/vivado_mcp/vivado/gui_session.py`。本机开发目录与 Python 安装目录下的对应文件均已修改。

相对上游基线，本地 Git 显示增加 118 行、删除 4 行，主要内容如下：

| 部位 | 上游行为 | 本机修改 |
| --- | --- | --- |
| 子进程命令 | 直接传入 `self.vivado_path` | 使用 `cmd.exe /c` 调用 Windows 批处理入口 |
| 输出流 | stdout/stderr 为 `DEVNULL` | 两者均改为 `asyncio.subprocess.PIPE` |
| 输出采集 | 不保留启动输出 | 新增两个后台读取任务，分别写入最多 200 条记录的 deque |
| 输出解码 | 无启动输出诊断 | 复用 `decode_vivado_output` |
| 失败消息 | 提前退出或连接超时提示 | 尝试附加两个输出流最近各 30 条记录 |
| 任务清理 | 无输出读取任务 | 在失败清理和 stop 中取消对应任务 |

保留的导入是 `from vivado_mcp.tcl_scripts import QUERY_CURRENT_PROJECT`，其中 `tcl_scripts` 为复数。

`cmd.exe /c` 只明确了 Windows 批处理调用方式，不能据此认为架构变量必然恢复。历史记录中，单独修改调用方式未解决启动故障；成功状态还包含下面的 loader 修改。当前 spawn 调用也没有单独传入 `env=spawn_env`。

本次比较结果：当前工作区与 `D:\mcp\vivado_mcp` 的源码文件 SHA256 相同；已安装副本的字节哈希不同，但 Git 按换行规范化比较没有代码差异。

### 4.2 Vivado 安装目录中的 loader.bat

该修改位于 Vivado 安装目录，**安装 Python 包不会自动应用它**。原文件已保存在同目录 `loader.bat.bak`。

原始架构判断：

```bat
set RDI_OS_ARCH=32
if [%PROCESSOR_ARCHITECTURE%] == [x86] (
  if defined PROCESSOR_ARCHITEW6432 (
    set RDI_OS_ARCH=64
  )
) else (
  if defined PROCESSOR_ARCHITECTURE (
    set RDI_OS_ARCH=64
  )
)
```

本机实际替换为：

```bat
rem Patched by user: force 64-bit (Python subprocess lacks PROC_ARCH env var)
set RDI_OS_ARCH=64
if [%PROCESSOR_ARCHITECTURE%] == [x86] (
  if not defined PROCESSOR_ARCHITEW6432 (
    set RDI_OS_ARCH=32
  )
)
```

效果是默认选择 64 位，只在检测到 `x86` 且没有 `PROCESSOR_ARCHITEW6432` 时回退为 32 位。这个判断依然依赖环境变量的真实性，不等于可靠识别所有机器架构。已验证范围是本机 Windows + Vivado 2018.3 的组合；不要把它无条件套用到其他版本、32 位或 ARM 环境。

### 4.3 MCP 客户端配置

此前会话记录中曾加入 Vivado 配置和 `PROCESSOR_*` 环境变量。**2026-09-18 检查当前 `C:\Users\Shirley\.codex\config.toml` 时，未找到 `[mcp_servers.vivado]`、`VIVADO_PATH` 或 `PROCESSOR_*` 条目。** 因此以下是按本机路径整理的恢复示例，不是当前文件的完整转储，也不代表当前会话已加载该配置。

```toml
[mcp_servers.vivado]
enabled = true
command = "D:/software/py3_13_5/systemfile/python.exe"
args = ["-m", "vivado_mcp"]

[mcp_servers.vivado.env]
VIVADO_PATH = "D:/software/vivado18_3/systemfile/Vivado/2018.3/bin/vivado.bat"
PROCESSOR_ARCHITECTURE = "AMD64"
PROCESSOR_ARCHITEW6432 = ""
PROCESSOR_IDENTIFIER = "AMD64 Family 25 Model 80 Stepping 0, AuthenticAMD"
PROCESSOR_LEVEL = "6"
PROCESSOR_REVISION = "0000"
```

这里保留 `PROCESSOR_*` 是为了记录此前配置。并未验证每一项都是必要条件；所修改的 loader 判断只涉及 `PROCESSOR_ARCHITECTURE` 和 `PROCESSOR_ARCHITEW6432`，不使用其余三项。换设备时不要照搬 CPU 标识和位数值，应按新环境设置并重新验证。

`command` 应指向安装了适配包的解释器，`VIVADO_PATH` 指向真实 Vivado 入口。配置文件中已有同名 TOML 表时应编辑原表，不能重复追加。修改后重启客户端或其 MCP 服务，使新的进程读取配置。

## 5. 在其他设备部署

1. 安装目标设备支持的 Vivado 和 Python。上游包声明 Python >= 3.10；本机实际验证版本是 Python 3.13.5 与 Vivado 2018.3。
2. 取得包含本地适配的完整源码。当前远端同步尚未完成，可先使用本地源码副本；以后从个人仓库克隆时，应检查 GUI 文件确实包含本文修改。不要用一次普通的 `pip install vivado-mcp` 假定获得本地改动。
3. 在新设备以实际路径运行以下示例，使用独立虚拟环境安装源码，避免同时手工维护开发副本和 site-packages 副本。

```powershell
Set-Location 'D:\mcp\vivado_mcp'
python -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -e .
& '.\.venv\Scripts\python.exe' -c "import vivado_mcp.vivado.gui_session as g; print(g.__file__)"
```

4. 将 MCP 配置的 `command` 改为新虚拟环境解释器的绝对路径，例如 `D:/mcp/vivado_mcp/.venv/Scripts/python.exe`，并填写新设备的 `VIVADO_PATH`。
5. 若新设备同样出现误选 `win32.o`，先确认其 Vivado 目录结构和原始 loader 判断，再备份并修改对应代码块。仅针对已确认的环境修改，不覆盖其他版本的整个 loader。安装目录受保护时可能需要管理员权限。

备份示例，先将路径改为目标设备路径；已有备份时不要覆盖：

```powershell
$vivadoBin = 'D:\software\vivado18_3\systemfile\Vivado\2018.3\bin'
$loaderBackup = Join-Path $vivadoBin 'loader.bat.bak'
if (-not (Test-Path -LiteralPath $loaderBackup)) {
    Copy-Item -LiteralPath (Join-Path $vivadoBin 'loader.bat') -Destination $loaderBackup
}
```

6. 如需连接手动启动的 Vivado，使用该环境解释器执行 `python -m vivado_mcp install` 对应命令，让 `Vivado_init.tcl` 在启动时加载 TCP 服务。这个 install 子命令用于注入 Tcl，并不等于安装 Python 包或配置 MCP 客户端。MCP 自行 spawn GUI 的路径已经通过 `-source` 临时注入服务，不以持久化 install 为必需条件。
7. 重启 MCP 服务，按下一节验证。新设备可能还需要独立配置 Vivado 许可证、器件支持和工程文件；本仓库不提供 Vivado 安装程序或许可证。

## 6. 使用与验收

下面是 MCP 工具调用示意，不是 PowerShell 命令；工具名称前缀由客户端决定。

```text
start_session(session_id="verify_gui", mode="gui", port=0, timeout=120)
run_tcl(session_id="verify_gui", command="version -short")
```

`port=0` 用于分配端口并启动独立新实例，便于验证 spawn 是否成功。默认端口为 9999，检测到已有服务时可能直接 attach；仅 attach 成功不能证明新建 GUI 的启动问题已修复。历史成功连接使用的 50683 是当时分配的端口，不需要写死。

预期看到 GUI 窗口、会话 ready，以及版本号 `2018.3`。Tcl 命令是 `version -short`，不是 `version -shot`。

随后通过同一会话的 `run_tcl` 执行：

```tcl
open_project {D:/zynq7100/RX/DSSS_RX_914/NO428_RX/DSSS_RX.xpr}
get_property NAME [current_project]
get_property PART [current_project]
```

历史结果为工程 `DSSS_RX`、器件 `xc7z100ffg900-2`。其他设备需替换工程路径，且本文不包含该工程。

只关闭工程、保留 Vivado 窗口时，在同一会话执行：

```tcl
close_project
```

`mode="tcl"` 指 Vivado 命令行会话，没有 GUI 窗口；`mode="gui"` 用于创建或复用 GUI；`mode="attach"` 连接已存在的 GUI 服务。不要用 `exit` 或停止由 MCP 创建的整个会话来代替 `close_project`。

## 7. 当前代码的已知限制

- 当前 `cmd.exe /c` 调用没有按操作系统分支，只适用于 Windows，不能宣称本地改版保持了上游 Linux 启动兼容性。
- 两个读取函数使用 `rstrip("\r\n")` 清理真实换行，两个拼接函数使用 `"\n".join(...)` 生成多行诊断；此前的字面反斜杠写法已在本文对应提交中修正。
- 当前实现使用 `readline()` 读取输出，且停止时先取消读取任务；尚未验证超长输出行、大量退出日志及所有异常清理场景。
- 源码内关于 `cmd.exe` 一定恢复架构变量的注释不作为已证实结论；已有成功记录不能单独证明该改动的作用。
- 未在本文生成过程中运行完整测试套件、重新启动 GUI，或验证其他 Vivado 版本。

## 8. 回滚与维护

修改前应保留原始文件。已有 `gui_session.py.bak`、`.bak2`、`.bak3` 可能对应不同排查阶段，不能只根据后缀认定它们是上游原版；恢复时先比较内容，或从明确的上游基线取得文件。

关闭相关 Vivado 进程后，确认 `loader.bat.bak` 是本机原版，再恢复：

```powershell
$vivadoBin = 'D:\software\vivado18_3\systemfile\Vivado\2018.3\bin'
Copy-Item -LiteralPath (Join-Path $vivadoBin 'loader.bat.bak') -Destination (Join-Path $vivadoBin 'loader.bat') -Force
```

Python 包回滚应使用 MCP 配置中的同一解释器，恢复原文件或安装明确的上游版本；仅恢复开发目录不能保证已安装副本同步恢复。客户端配置只恢复自己修改过的 Vivado 条目，保留其他服务设置。执行过 Tcl 持久化安装时，可用同一解释器运行 `-m vivado_mcp uninstall` 移除该注入。

Vivado 更新或修复安装可能重置 loader，需要重新检查。发布个人仓库时应保留上游许可证、完整源码、Tcl 服务脚本和测试，不上传完整用户配置、Token、虚拟环境或商业 Vivado 安装目录。

本次 GitHub 连接中的 `schannel: SEC_E_NO_CREDENTIALS` 是 TLS 层错误，不能直接判定 GitHub Token 失效；自动审批服务的 `503 / model_not_found` 是另一个独立阻塞。这两项与 Vivado GUI 适配本身无关。

