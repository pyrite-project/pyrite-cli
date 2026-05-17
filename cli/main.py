import os
import typer
import click
import sys
import re
import subprocess
from typing import List, Optional
from .utils.flash import MicroPython
from .utils.webrepl_micropython import WebREPLMicroPython
from .utils.config import create_default_config
from .project.project import init_project, init_stubs, new_project_interactive
from .project.sync import ProjectSyncManager
from .utils.firmware import flash_firmware, erase_flash, chip_info, verify_firmware, read_flash


def _norm_path(p: str) -> str:
    """修复 MSYS2（Git Bash）路径转换问题。

    Git Bash 会将 Unix 风格 /xxx 参数自动转换为 Windows 绝对路径
    （如 /lib → D:/Program Files/Git/lib），导致 MicroPython 设备路径错误。
    检测到被转换的路径时，恢复原始设备路径。
    """
    if not isinstance(p, str) or not re.match(r'^[A-Za-z]:[/\\]', p):
        return p

    # 从 PATH 中推断 MSYS2 根目录（如 D:\Program Files\Git）
    msys_root = None
    for entry in os.environ.get('PATH', '').split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        norm = entry.replace('/', os.sep)
        # MSYS2 的 bin 目录特征：mingw64/bin, usr/bin 等
        if norm.rstrip('\\').endswith('mingw64\\bin') or norm.rstrip('\\').endswith('usr\\bin'):
            # 取父目录的父目录作为根
            parent = os.path.dirname(os.path.dirname(norm))
            if re.match(r'^[A-Za-z]:[/\\]', parent):
                msys_root = parent
                break

    if msys_root is None:
        # 无法确定 MSYS2 根，保守处理：只针对裸驱动器路径
        if re.match(r'^[A-Za-z]:[/\\]$', p):
            typer.secho(f"  [WARN] 路径 '{p}' 被 MSYS2 转换，已恢复为 '/'",
                        fg=typer.colors.YELLOW)
            return '/'
        return p

    # 去掉 MSYS2 根前缀，恢复原始 /xxx 路径
    p_norm = p.replace('/', '\\')
    prefix = msys_root.rstrip('\\') + '\\'
    if p_norm.startswith(prefix):
        rest = p_norm[len(prefix):].replace('\\', '/')
        recovered = '/' + rest
        if recovered != p:
            typer.secho(f"  [WARN] 路径 '{p}' 被 MSYS2 转换，已恢复为 '{recovered}'",
                        fg=typer.colors.YELLOW)
        return recovered

    return p

def _complete_port(ctx: click.Context, args: List[str], incomplete: str) -> List[str]:
    """Shell 补全回调：自动补全可用串口号。"""
    try:
        ports = MicroPython.scan_ports(require_vid=False)
        return [p["device"] for p in ports if incomplete in p["device"]]
    except Exception:
        return []


app = typer.Typer(
    name="pyrite-cli",
    help="## PYRITE-CLI ## - MicroPython 设备刷入工具",
    add_completion=True,
)


_BRIEF_CODE = """\
import sys,os,machine
u=os.uname()
print(sys.implementation.name+' '+'.'.join(str(x) for x in sys.implementation.version))
print(u.machine)
print(str(machine.freq()//1000000)+' MHz')
"""

def _fetch_brief(port: str) -> str:
    mp = MicroPython(port=port)
    try:
        mp.connect()
        out = mp.run(_BRIEF_CODE)
    except Exception:
        return ""
    finally:
        mp.disconnect()
    lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
    return "  " + "  ".join(lines) if lines else ""


def _mp_factory(port: str, baudrate: int, timeout: int,
                webrepl: Optional[str] = None,
                password: Optional[str] = None) -> MicroPython:
    """创建 MicroPython 实例，支持串口和 WebREPL。"""
    if webrepl:
        return WebREPLMicroPython(url=webrepl, password=password, timeout=timeout)
    return MicroPython(port=port, baudrate=baudrate, timeout=timeout)


@app.command()
def scan(
    vid: Optional[int] = typer.Option(None, "--vid", help="按 VID 过滤（十进制）"),
    pid: Optional[int] = typer.Option(None, "--pid", help="按 PID 过滤（十进制）"),
    keyword: Optional[str] = typer.Option(None, "--keyword", "-k", help="按描述关键字过滤"),
    all: bool = typer.Option(False, "--all", "-a", help="显示所有设备（包括无 VID/PID 的）"),
    with_info: bool = typer.Option(False, "--with-info", "-i", help="连接设备并显示简略板子信息"),
):
    """扫描主机所有可用串口设备"""
    ports = MicroPython.scan_ports(vid=vid, pid=pid, keyword=keyword, require_vid=not all)
    if not ports:
        print("未检测到串口设备。")
        raise typer.Exit()
    print(f"发现 {len(ports)} 个串口设备:\n")
    for p in ports:
        tags = []
        if p["vid"] is not None:
            tags.append(f"VID={p['vid']:04X}")
        if p["pid"] is not None:
            tags.append(f"PID={p['pid']:04X}")
        sn = f" S/N={p['serial_number']}" if p["serial_number"] else ""
        tag_str = f" ({', '.join(tags)}{sn})" if tags else ""
        print(f"  {p['device']}{tag_str}")
        print(f"    {p['description']}")
        if with_info:
            brief = _fetch_brief(p["device"])
            if brief:
                typer.secho(brief, fg=typer.colors.BRIGHT_BLACK)


@app.command()
def flash(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    file: str = typer.Argument(..., help="待刷入的本地文件路径"),
    remote_path: str = typer.Argument(..., help="设备上的目标路径"),
    baudrate: int = typer.Option(115200, "--baudrate", "-b",
                                 help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t",
                                help="超时秒数（默认 10）"),
    no_compile: bool = typer.Option(False, "--no-compile", help="跳过 mpy 编译，刷入原始 .py 文件"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target（离线时使用）"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags，逗号分隔"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags，逗号分隔"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL, 如 ws://192.168.4.1:8266"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
):
    """连接设备并通过原始 REPL 刷入单个文件"""
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        ver, arch = mp.get_mpy_version() if not no_compile else (None, None)
        if target:
            active_tags = set(mp.config["board_tags"].get(target.upper(), [target.upper()]))
            active_tags.add(target.upper())
        else:
            active_tags = mp.detect_tags()
            if not active_tags:
                typer.secho("无法识别设备 target，请使用 --target 手动指定", fg=typer.colors.RED)
                raise typer.Exit(1)
        if feature:
            active_tags.update(t.strip() for t in feature.split(","))
        if no_feature:
            active_tags.difference_update(t.strip() for t in no_feature.split(","))
        mp.flash_file(file, remote_path, compile=not no_compile, bytecode_ver=ver, arch=arch, active_tags=active_tags or None)
    finally:
        mp.disconnect()

@app.command()
def repl(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    baudrate: int = typer.Option(115200, "--baudrate", "-b",
                                 help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t",
                                help="超时秒数（默认 10）"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL, 如 ws://192.168.4.1:8266"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
):
    """连接设备并进入交互式 REPL 终端"""
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        mp.repl_()
    finally:
        mp.disconnect()


@app.command()
def flash_program(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    directory: str = typer.Argument(..., help="本地目录路径"),
    remote_path: str = typer.Argument(..., help="设备上的远程路径前缀"),
    baudrate: int = typer.Option(115200, "--baudrate", "-b",
                                 help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t",
                                help="超时秒数（默认 10）"),
    no_compile: bool = typer.Option(False, "--no-compile", help="跳过 mpy 编译"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target（离线时使用）"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags，逗号分隔"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags，逗号分隔"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL, 如 ws://192.168.4.1:8266"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
):
    """连接设备并递归刷入整个本地目录"""
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if no_compile:
            mp.config.auto_compile = False
        ver, arch = mp.get_mpy_version() if not no_compile else (None, None)
        if target:
            active_tags = set(mp.config["board_tags"].get(target.upper(), [target.upper()]))
            active_tags.add(target.upper())
        else:
            active_tags = mp.detect_tags()
            if not active_tags:
                typer.secho("无法识别设备 target，请使用 --target 手动指定", fg=typer.colors.RED)
                raise typer.Exit(1)
        if feature:
            active_tags.update(t.strip() for t in feature.split(","))
        if no_feature:
            active_tags.difference_update(t.strip() for t in no_feature.split(","))
        results = mp.flash_program(directory, remote_path, bytecode_ver=ver, arch=arch,
                                   active_tags=active_tags or None, manifest_path=manifest)
        ok = sum(1 for _, _, s in results if s)
        fail = sum(1 for _, _, s in results if not s)
        parts = []
        if ok:
            parts.append(f"\033[32m{ok} 成功\033[0m")
        if fail:
            parts.append(f"\033[31m{fail} 失败\033[0m")
        print(f"\n完成: {', '.join(parts)}")
    finally:
        mp.disconnect()


@app.command()
def run(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    code: str = typer.Argument(..., help="要执行的 Python 代码"),
    baudrate: int = typer.Option(115200, "--baudrate", "-b",
                                 help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t",
                                help="超时秒数（默认 10）"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL, 如 ws://192.168.4.1:8266"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
):
    """连接设备并执行一行 Python 代码，打印输出"""
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        output = mp.run(code)
        if output:
            print(output)
    finally:
        mp.disconnect()


@app.command()
def reset(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    baudrate: int = typer.Option(115200, "--baudrate", "-b",
                                 help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t",
                                help="超时秒数（默认 10）"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL, 如 ws://192.168.4.1:8266"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
):
    """连接设备并通过原始 REPL 软重启"""
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        mp.reset()
        print("设备已重启。")
    finally:
        mp.disconnect()

@app.command()
def config():
    """在当前目录生成默认 .pyrite_config.json 配置文件"""
    create_default_config()


@app.command()
def board_info(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    baudrate: int = typer.Option(115200, "--baudrate", "-b", help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数（默认 10）"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL, 如 ws://192.168.4.1:8266"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
):
    """连接设备并获取详细板级信息（固件、CPU、内存、Flash 等）"""
    code = """\
import sys,os,gc,machine,ubinascii
u=os.uname()
st=os.statvfs('/')
print('FW:'+sys.implementation.name+' '+'.'.join(str(x) for x in sys.implementation.version))
print('PLAT:'+sys.platform)
print('HW:'+u.machine)
print('REL:'+u.release)
print('CPU:'+str(machine.freq()))
print('UID:'+ubinascii.hexlify(machine.unique_id()).decode())
rc=machine.reset_cause()
_RC={getattr(machine,n):n for n in('PWRON_RESET','HARD_RESET','WDT_RESET','DEEPSLEEP_RESET','SOFT_RESET')if hasattr(machine,n)}
print('RST:'+_RC.get(rc,str(rc)))
gc.collect()
print('MF:'+str(gc.mem_free()))
print('MA:'+str(gc.mem_alloc()))
print('FS:'+str(st[0]*st[2])+'/'+str(st[0]*st[3]))
try:
 import esp
 print('FLASH:'+str(esp.flash_size()))
except:pass
try:
 import network
 w=network.WLAN(network.STA_IF)
 w.active(True)
 print('MAC:'+':'.join('%02x'%b for b in w.config('mac')))
except:pass
"""
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        output = mp.run(code)
    finally:
        mp.disconnect()

    if not output:
        typer.secho("未获取到设备信息。", fg=typer.colors.RED)
        return

    info = {}
    for line in output.strip().splitlines():
        if ':' in line:
            k, _, v = line.partition(':')
            info[k] = v

    def row(label: str, value: str):
        # 中文字符占2列，补齐到10列宽
        pad = 10 - sum(2 if ord(c) > 127 else 1 for c in label)
        typer.secho(f"  {label}{' ' * pad}", fg=typer.colors.BRIGHT_BLACK, nl=False)
        typer.echo(value)

    def section(title: str):
        typer.echo()
        typer.secho(f"── {title} ", fg=typer.colors.BRIGHT_CYAN, bold=True)

    section("固件")
    row("名称", info.get('FW', '?'))
    row("平台", info.get('PLAT', '?'))
    row("硬件", info.get('HW', '?'))
    row("版本", info.get('REL', '?'))

    section("设备")
    if 'CPU' in info:
        row("CPU", f"{int(info['CPU'])//1_000_000} MHz")
    row("唯一ID", info.get('UID', '?'))
    row("复位原因", info.get('RST', '?'))
    if 'MAC' in info:
        row("MAC", info['MAC'])

    section("内存")
    if 'MF' in info and 'MA' in info:
        mf, ma = int(info['MF']), int(info['MA'])
        row("RAM", f"{ma//1024} KB used / {(mf+ma)//1024} KB total")
    if 'FS' in info:
        total, free = info['FS'].split('/')
        row("Flash FS", f"{(int(total)-int(free))//1024} KB used / {int(total)//1024} KB total")
    if 'FLASH' in info:
        row("Flash", f"{int(info['FLASH'])//1024} KB")
    typer.echo()


# ── project 子命令组 ──────────────────────────────────────────────

project_app = typer.Typer(help="项目脚手架、存根、文件哈希与增量刷入",
                          add_completion=False)
app.add_typer(project_app, name="project")


@project_app.command("new")
def project_new(
    project_name: str = typer.Argument(..., help="新项目名称"),
    platform: Optional[str] = typer.Option(None, "--platform",
                                           help="串口号，用于自动检测硬件并下载匹配的 stubs"),
):
    """创建新 MicroPython 项目目录及脚手架"""
    new_project_interactive(project_name, platform=platform)


@project_app.command("init")
def project_init(
    hardware: Optional[str] = typer.Argument(None, help="MicroPython 硬件名称"),
    version: Optional[str] = typer.Argument(None,
                                            help="固件版本，如 '1.20.0'"),
    variant: Optional[str] = typer.Option(None, "--variant", "-V",
                                          help="硬件变体，如 ESP32_GENERIC"),
    platform: Optional[str] = typer.Option(None, "--platform",
                                           help="串口号，用于自动检测硬件并下载匹配的 stubs"),
):
    """在已有项目中下载 MicroPython 类型存根"""
    init_stubs(hardware, version, variant, platform)


@project_app.command("hash")
def project_hash(
    directory: str = typer.Argument(".", help="项目目录路径"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="激活的 feature tags，逗号分隔（用于条件编译过滤）"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags，逗号分隔"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="哈希配置文件输出路径"),
):
    """离线扫描项目目录，计算 SHA256 哈希并保存到哈希配置文件"""
    mp = MicroPython()
    active_tags = set()
    if feature:
        active_tags.update(t.strip() for t in feature.split(","))
    if no_feature:
        active_tags.difference_update(t.strip() for t in no_feature.split(","))
    ProjectSyncManager(mp).scan(directory, hash_config_path=output,
                       active_tags=active_tags or None, manifest_path=manifest)


@project_app.command("flash")
def project_flash(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    directory: str = typer.Argument(..., help="本地项目目录路径"),
    remote_path: str = typer.Argument(..., help="设备上的远程路径前缀"),
    baudrate: int = typer.Option(115200, "--baudrate", "-b",
                                 help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t",
                                help="超时秒数（默认 10）"),
    no_compile: bool = typer.Option(False, "--no-compile", help="跳过 mpy 编译"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target（离线时使用）"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags，逗号分隔"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags，逗号分隔"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    hash_config: Optional[str] = typer.Option(None, "--config", "-c", help="哈希配置文件路径"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL, 如 ws://192.168.4.1:8266"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
):
    """连接设备并根据哈希配置增量刷入新增或变更的文件"""
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if no_compile:
            mp.config.auto_compile = False
        ver, arch = mp.get_mpy_version() if not no_compile else (None, None)
        if target:
            active_tags = set(mp.config["board_tags"].get(target.upper(), [target.upper()]))
            active_tags.add(target.upper())
        else:
            active_tags = mp.detect_tags()
            if not active_tags:
                typer.secho("无法识别设备 target，请使用 --target 手动指定", fg=typer.colors.RED)
                raise typer.Exit(1)
        if feature:
            active_tags.update(t.strip() for t in feature.split(","))
        if no_feature:
            active_tags.difference_update(t.strip() for t in no_feature.split(","))
        ProjectSyncManager(mp).flash(directory, remote_path, hash_config_path=hash_config,
                            bytecode_ver=ver, arch=arch,
                            active_tags=active_tags or None, manifest_path=manifest)
    finally:
        mp.disconnect()


@project_app.command("status")
def project_status(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    directory: str = typer.Argument(..., help="本地项目目录路径"),
    remote_path: str = typer.Argument(..., help="设备上的远程路径前缀"),
    baudrate: int = typer.Option(115200, "--baudrate", "-b",
                                 help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t",
                                help="超时秒数（默认 10）"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target（离线时使用）"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags，逗号分隔"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags，逗号分隔"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    hash_config: Optional[str] = typer.Option(None, "--config", "-c", help="哈希配置文件路径"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL, 如 ws://192.168.4.1:8266"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
):
    """连接设备并比对本地哈希与设备文件，显示差异清单（不刷入）"""
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if target:
            active_tags = set(mp.config["board_tags"].get(target.upper(), [target.upper()]))
            active_tags.add(target.upper())
        else:
            active_tags = mp.detect_tags()
        if feature:
            active_tags.update(t.strip() for t in feature.split(","))
        if no_feature:
            active_tags.difference_update(t.strip() for t in no_feature.split(","))
        ProjectSyncManager(mp).status(directory, remote_path, hash_config_path=hash_config,
                             active_tags=active_tags or None, manifest_path=manifest)
    finally:
        mp.disconnect()


@project_app.command("pull")
def project_pull(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    directory: str = typer.Argument(help="本地项目目录路径（如 . 或 ./bak）"),
    remote_path: str = typer.Argument("/",
                                      help="设备上的远程路径前缀",
                                      show_default=False),
    baudrate: int = typer.Option(115200, "--baudrate", "-b", help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数（默认 10）"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target（离线时使用）"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags，逗号分隔"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags，逗号分隔"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="预览模式：仅列出待下载文件，不实际拉取"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL, 如 ws://192.168.4.1:8266"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
):
    """连接设备并按项目配置拉取文件到本地目录"""
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if target:
            active_tags = set(mp.config["board_tags"].get(target.upper(), [target.upper()]))
            active_tags.add(target.upper())
        else:
            active_tags = None
        if feature:
            active_tags = set(t.strip() for t in feature.split(","))
        if no_feature:
            if active_tags is None:
                active_tags = set()
            active_tags.difference_update(t.strip() for t in no_feature.split(","))
        ProjectSyncManager(mp).pull(directory, remote_path,
                           active_tags=active_tags, manifest_path=manifest,
                           dry_run=dry_run)
    finally:
        mp.disconnect()


# ── fs 设备文件浏览器 ──────────────────────────────────────────────

fs_app = typer.Typer(help="MicroPython 设备文件浏览器",
                     add_completion=False)
app.add_typer(fs_app, name="fs")


def _display_paged(lines_with_color: List[tuple[str, bool]], page_size: int = 20) -> None:
    """分页显示文件列表，按 Enter 继续，按 q 退出。"""
    total = len(lines_with_color)
    start = 0
    while start < total:
        end = min(start + page_size, total)
        for i in range(start, end):
            line, is_dir = lines_with_color[i]
            if is_dir:
                typer.secho(line, fg=typer.colors.YELLOW)
            else:
                print(line)
        start = end
        if start < total:
            typer.secho(
                f"\n  -- 更多 ({start}/{total} 行, Enter 继续, q 退出) -- ",
                fg=typer.colors.BRIGHT_BLACK, nl=False
            )
            ch = _read_one_key()
            print()
            if ch == 'q':
                break


def _read_one_key() -> str:
    """读取单键输入，跨平台。返回 'q' 或 'enter'。"""
    try:
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b'q', b'Q'):
            return 'q'
        return 'enter'
    except ImportError:
        import sys
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if ch in ('q', 'Q'):
            return 'q'
        return 'enter'


def _build_tag_args(mp: MicroPython, target: Optional[str],
                    feature: Optional[str], no_feature: Optional[str]) -> set[str]:
    """构建 active_tags 公共逻辑。"""
    if target:
        active_tags: set[str] = set(mp.config.board_tags.get(target.upper(), [target.upper()]))
        active_tags.add(target.upper())
    else:
        active_tags = mp.detect_tags()
    if feature:
        active_tags.update(t.strip() for t in feature.split(","))
    if no_feature:
        active_tags.difference_update(t.strip() for t in no_feature.split(","))
    return active_tags


@fs_app.command("ls")
def fs_ls(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    path: str = typer.Argument("/", help="设备上的目录路径"),
    recursive: bool = typer.Option(False, "--recursive", "-r",
                                   help="递归列出所有子目录"),
    sort: Optional[str] = typer.Option(None, "--sort",
                                        help="排序方式: name, size, type, -name, -size, -type（加 - 为倒序）"),
    paginate: bool = typer.Option(False, "--paginate", "-p",
                                   help="分页显示（每页 20 行）"),
    baudrate: int = typer.Option(115200, "--baudrate", "-b",
                                 help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t",
                                help="超时秒数（默认 10）"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL, 如 ws://192.168.4.1:8266"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
):
    """连接设备并列出指定目录的内容"""
    path = _norm_path(path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if recursive:
            items = mp.fs_ls_recursive(path)
        else:
            items = mp.fs_ls(path)
        if not items:
            print("  (空目录)")
        else:
            # 排序
            reverse = False
            sort_key = (sort or "name")
            if sort_key.startswith("-"):
                reverse = True
                sort_key = sort_key[1:]

            if sort_key == "size":
                items.sort(key=lambda x: int(x['size']) if x['size'].isdigit() else 0, reverse=reverse)
            elif sort_key == "type":
                items.sort(key=lambda x: (x['type'], x['name']), reverse=reverse)
            else:  # name（默认）
                items.sort(key=lambda x: x['name'], reverse=reverse)

            # 格式化输出行
            output_lines = []
            for item in items:
                is_dir = item['type'] == 'D'
                name = item['name'] + '/' if is_dir else item['name']
                sz = item['size'] if item['size'].isdigit() else '?'
                if sz.isdigit():
                    sz_int = int(sz)
                    if sz_int < 1024:
                        sz_str = f"{sz_int:>7} bytes"
                    else:
                        sz_str = f" {sz_int // 1024:>6} KB"
                else:
                    sz_str = f" {sz:>7}"
                line = f"  {'[D]' if is_dir else '[F]'} {name:<31} {sz_str}"
                output_lines.append((line, is_dir))

            # 输出（分页 / 直接）
            if paginate and len(output_lines) > 20:
                _display_paged(output_lines, page_size=20)
            else:
                for line, is_dir in output_lines:
                    if is_dir:
                        typer.secho(line, fg=typer.colors.YELLOW)
                    else:
                        print(line)

        # 仅在非递归且列根目录时显示 Flash 占用进度条
        if not recursive and path.strip() in ("", ".", "./", "/"):
            df = mp.fs_df()
            if df['total'] > 0:
                pct = df['used'] / df['total']
                bar_w = 30
                filled = int(bar_w * pct)
                bar = '█' * filled + '░' * (bar_w - filled)
                total_mb = df['total'] / 1024 / 1024
                used_mb = df['used'] / 1024 / 1024
                free_mb = df['free'] / 1024 / 1024
                typer.secho(f"\n  Flash: [{bar}] {pct * 100:.1f}%", fg=typer.colors.BRIGHT_BLACK)
                print(f"         {used_mb:.1f} MB used / {free_mb:.1f} MB free / {total_mb:.1f} MB total")
    finally:
        mp.disconnect()


@fs_app.command("rm")
def fs_rm(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    path: str = typer.Argument(..., help="设备上要删除的文件路径"),
    baudrate: int = typer.Option(115200, "--baudrate", "-b",
                                 help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t",
                                help="超时秒数（默认 10）"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL, 如 ws://192.168.4.1:8266"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
):
    """连接设备并删除指定文件"""
    path = _norm_path(path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        if mp.fs_rm(path):
            print(f"  已删除: {path}")
        else:
            print(f"  删除失败: {path}")
    finally:
        mp.disconnect()


@fs_app.command("cat")
def fs_cat(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    path: str = typer.Argument(..., help="设备上的文件路径"),
    baudrate: int = typer.Option(115200, "--baudrate", "-b",
                                 help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t",
                                help="超时秒数（默认 10）"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL, 如 ws://192.168.4.1:8266"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
):
    """连接设备并打印指定文本文件的内容"""
    path = _norm_path(path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        print(mp.fs_cat(path))
    finally:
        mp.disconnect()


@fs_app.command("put")
def fs_put(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    local_path: str = typer.Argument(..., help="本地文件路径"),
    remote_path: str = typer.Argument(..., help="设备上的目标路径"),
    baudrate: int = typer.Option(115200, "--baudrate", "-b",
                                 help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t",
                                help="超时秒数（默认 10）"),
    no_compile: bool = typer.Option(False, "--no-compile", help="跳过 mpy 编译"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target（离线时使用）"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 feature tags，逗号分隔"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 feature tags，逗号分隔"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL, 如 ws://192.168.4.1:8266"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
):
    """连接设备并上传本地文件"""
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        ver, arch = mp.get_mpy_version() if not no_compile else (None, None)
        active_tags = _build_tag_args(mp, target, feature, no_feature)
        if not active_tags and not target:
            typer.secho("无法识别设备 target，请使用 --target 手动指定", fg=typer.colors.RED)
            raise typer.Exit(1)
        mp.flash_file(local_path, remote_path, compile=not no_compile,
                      bytecode_ver=ver, arch=arch, active_tags=active_tags or None)
    finally:
        mp.disconnect()


@fs_app.command("get")
def fs_get(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    remote_path: str = typer.Argument(..., help="设备上的文件路径"),
    local_path: str = typer.Argument(None, help="本地保存路径（默认使用远程文件名）"),
    baudrate: int = typer.Option(115200, "--baudrate", "-b",
                                 help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t",
                                help="超时秒数（默认 10）"),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL, 如 ws://192.168.4.1:8266"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
):
    """连接设备并下载指定文件到本地"""
    remote_path = _norm_path(remote_path)
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        dst = local_path or os.path.basename(remote_path)
        sz = mp.fs_get(remote_path, dst)
        print(f"  已下载: {remote_path} -> {dst} ({sz} 字节)")
    finally:
        mp.disconnect()


# ── firmware 固件刷写 ────────────────────────────────────────────

firmware_app = typer.Typer(help="固件刷写工具（需安装 esptool）",
                           add_completion=False)
app.add_typer(firmware_app, name="firmware")


@firmware_app.command("flash")
def firmware_flash(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    firmware: str = typer.Argument(..., help="固件 .bin 文件路径"),
    baudrate: int = typer.Option(460800, "--baud", "-b", help="波特率（默认 460800）"),
    address: str = typer.Option("0x0", "--address", "-a", help="烧录起始地址（默认 0x0）"),
    flash_mode: str = typer.Option("keep", "--flash-mode", "-m",
                                    help="Flash 模式: qio, qout, dio, dout, keep"),
    flash_size: str = typer.Option("keep", "--flash-size", "-s",
                                    help="Flash 容量: 1MB/2MB/4MB/8MB/16MB/detect/keep"),
    erase_first: bool = typer.Option(False, "--erase-first", "-e",
                                      help="烧录前先全片擦除"),
):
    """通过 esptool 烧录固件 .bin 到设备"""
    try:
        flash_firmware(
            port=port, firmware=firmware, baudrate=baudrate,
            address=address, flash_mode=flash_mode, flash_size=flash_size,
            erase_first=erase_first,
        )
        typer.secho("  ✓ 烧录完成", fg=typer.colors.GREEN)
    except FileNotFoundError:
        typer.secho("未找到 esptool，请安装：pip install esptool", fg=typer.colors.RED)
        raise typer.Exit(1)
    except subprocess.CalledProcessError:
        typer.secho("  ✗ 烧录失败，请检查连接和参数", fg=typer.colors.RED)
        raise typer.Exit(1)


@firmware_app.command("erase")
def firmware_erase(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    baudrate: int = typer.Option(460800, "--baud", "-b", help="波特率（默认 460800）"),
):
    """通过 esptool 擦除设备整个 Flash"""
    try:
        erase_flash(port=port, baudrate=baudrate)
        typer.secho("  ✓ Flash 已擦除", fg=typer.colors.GREEN)
    except FileNotFoundError:
        typer.secho("未找到 esptool，请安装：pip install esptool", fg=typer.colors.RED)
        raise typer.Exit(1)
    except subprocess.CalledProcessError:
        typer.secho("  ✗ 擦除失败，请检查连接", fg=typer.colors.RED)
        raise typer.Exit(1)


@firmware_app.command("info")
def firmware_info(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    baudrate: int = typer.Option(460800, "--baud", "-b", help="波特率（默认 460800）"),
):
    """通过 esptool 读取设备芯片和 Flash 信息"""
    try:
        output = chip_info(port=port, baudrate=baudrate)
        # 提取关键字段用于格式化输出
        lines = output.strip().splitlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # 去掉 esptool 的 DEBUG/日志前缀，只显示关键行
            if any(kw in line for kw in ("Detected", "Manufacturer", "Device",
                                         "flash size", "MAC:", "Chip is",
                                         "Features:", "Crystal")):
                typer.secho(f"  {line}", fg=typer.colors.CYAN)
            elif line.startswith("esptool.py") or "Serial" in line:
                print(f"  {line}")
            else:
                print(f"  {line}")
    except FileNotFoundError:
        typer.secho("未找到 esptool，请安装：pip install esptool", fg=typer.colors.RED)
        raise typer.Exit(1)
    except RuntimeError as e:
        typer.secho(f"  ✗ {e}", fg=typer.colors.RED)
        raise typer.Exit(1)


@firmware_app.command("verify")
def firmware_verify(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    firmware: str = typer.Argument(..., help="固件 .bin 文件路径"),
    baudrate: int = typer.Option(460800, "--baud", "-b", help="波特率（默认 460800）"),
    address: str = typer.Option("0x0", "--address", "-a", help="起始地址（默认 0x0）"),
):
    """通过 esptool 验证固件烧录结果（比对 Flash 内容）"""
    try:
        verify_firmware(port=port, firmware=firmware, baudrate=baudrate, address=address)
        typer.secho("  ✓ 验证通过，固件与 Flash 内容一致", fg=typer.colors.GREEN)
    except FileNotFoundError:
        typer.secho("未找到 esptool，请安装：pip install esptool", fg=typer.colors.RED)
        raise typer.Exit(1)
    except subprocess.CalledProcessError:
        typer.secho("  ✗ 验证失败，固件与 Flash 内容不匹配", fg=typer.colors.RED)
        raise typer.Exit(1)


@firmware_app.command("read")
def firmware_read(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0",
                               autocompletion=_complete_port),
    size: str = typer.Argument(..., help="读取字节数（如 0x100000）"),
    address: str = typer.Option("0x0", "--address", "-a", help="起始地址（默认 0x0）"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="输出文件路径"),
    baudrate: int = typer.Option(460800, "--baud", "-b", help="波特率（默认 460800）"),
):
    """通过 esptool 从设备 Flash 读取内容到文件"""
    try:
        dst = read_flash(port=port, size=size, address=address, output=output, baudrate=baudrate)
        typer.secho(f"  ✓ 已读取到: {dst}", fg=typer.colors.GREEN)
    except FileNotFoundError:
        typer.secho("未找到 esptool，请安装：pip install esptool", fg=typer.colors.RED)
        raise typer.Exit(1)
    except subprocess.CalledProcessError:
        typer.secho("  ✗ 读取失败", fg=typer.colors.RED)
        raise typer.Exit(1)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    app()


if __name__ == "__main__":
    main()
