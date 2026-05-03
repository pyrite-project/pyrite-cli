import typer
from typing import Optional
from .utils.Flash import MicroPython, create_default_config
from .project.project import init_project, init_stubs, new_project_interactive

app = typer.Typer(
    name="pyrite-cli",
    help="## PYRITE-CLI ## - MicroPython 设备刷入工具",
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


@app.command()
def scan(
    vid: Optional[int] = typer.Option(None, "--vid", help="按 VID 过滤（十进制）"),
    pid: Optional[int] = typer.Option(None, "--pid", help="按 PID 过滤（十进制）"),
    keyword: Optional[str] = typer.Option(None, "--keyword", "-k", help="按描述关键字过滤"),
    all: bool = typer.Option(False, "--all", "-a", help="显示所有设备（包括无 VID/PID 的）"),
    with_info: bool = typer.Option(False, "--with-info", "-i", help="连接设备并显示简略板子信息"),
):
    """扫描所有可用串口设备"""
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
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0"),
    file: str = typer.Argument(..., help="本地文件路径"),
    remote: Optional[str] = typer.Option(None, "--remote", "-r",
                                         help="设备上的目标路径（默认使用文件名）"),
    baudrate: int = typer.Option(115200, "--baudrate", "-b",
                                 help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t",
                                help="超时秒数（默认 10）"),
    no_compile: bool = typer.Option(False, "--no-compile", help="跳过编译，刷入原始 .py"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target（离线时使用）"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 tags，逗号分隔"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 tags，逗号分隔"),
):
    """刷入单个文件到设备"""
    mp = MicroPython(port=port, baudrate=baudrate, timeout=timeout)
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
        mp.flash_file(file, remote, compile=not no_compile, bytecode_ver=ver, arch=arch, active_tags=active_tags or None)
    finally:
        mp.disconnect()

@app.command()
def repl(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0"),
    baudrate: int = typer.Option(115200, "--baudrate", "-b",
                                 help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t",
                                help="超时秒数（默认 10）"),
):
    """连接MicroPython设备REPL"""
    mp = MicroPython(port=port, baudrate=baudrate, timeout=timeout)
    try:
        mp.connect()
        mp.repl_()
    finally:
        mp.disconnect()


@app.command()
def flash_program(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0"),
    directory: str = typer.Argument(..., help="本地目录路径"),
    prefix: Optional[str] = typer.Option(None, "--prefix", "-p",
                                         help="设备上的远程路径前缀"),
    baudrate: int = typer.Option(115200, "--baudrate", "-b",
                                 help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t",
                                help="超时秒数（默认 10）"),
    no_compile: bool = typer.Option(False, "--no-compile", help="跳过编译"),
    target: Optional[str] = typer.Option(None, "--target", help="手动指定 board target（离线时使用）"),
    feature: Optional[str] = typer.Option(None, "--feature", "-f", help="追加激活的 tags，逗号分隔"),
    no_feature: Optional[str] = typer.Option(None, "--no-feature", help="强制禁用的 tags，逗号分隔"),
    manifest: Optional[str] = typer.Option(None, "--manifest", "-m", help="manifest.py 路径"),
):
    """刷入整个目录到设备"""
    mp = MicroPython(port=port, baudrate=baudrate, timeout=timeout)
    try:
        mp.connect()
        if no_compile:
            mp.config["auto_compile"] = False
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
        results = mp.flash_program(directory, prefix or "", bytecode_ver=ver, arch=arch,
                                   active_tags=active_tags or None, manifest_path=manifest)
        ok = sum(1 for _, _, s in results if s)
        fail = sum(1 for _, _, s in results if not s)
        print(f"\n完成: {ok} 成功, {fail} 失败")
    finally:
        mp.disconnect()


@app.command()
def run(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0"),
    code: str = typer.Argument(..., help="要执行的 Python 代码"),
    baudrate: int = typer.Option(115200, "--baudrate", "-b",
                                 help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t",
                                help="超时秒数（默认 10）"),
):
    """在设备上执行 Python 代码"""
    mp = MicroPython(port=port, baudrate=baudrate, timeout=timeout)
    try:
        mp.connect()
        output = mp.run(code)
        if output:
            print(output)
    finally:
        mp.disconnect()


@app.command()
def reset(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0"),
    baudrate: int = typer.Option(115200, "--baudrate", "-b",
                                 help="波特率（默认 115200）"),
    timeout: int = typer.Option(10, "--timeout", "-t",
                                help="超时秒数（默认 10）"),
):
    """软重启 MicroPython 设备"""
    mp = MicroPython(port=port, baudrate=baudrate, timeout=timeout)
    try:
        mp.connect()
        mp.reset()
        print("设备已重启。")
    finally:
        mp.disconnect()

@app.command()
def new(
    project_name: str = typer.Argument(..., help="创建项目名称"),
    platform: Optional[str] = typer.Option(None, "--platform",
                                           help="串口号，如 COM3 或 /dev/ttyUSB0，自动检测硬件并下载对应 stubs"),
):
    """创建新 MicroPython 项目"""
    new_project_interactive(project_name, platform=platform)

@app.command()
def init(
    hardware: Optional[str] = typer.Argument(None,
                                             help="使用的 MicroPython 硬件名称（使用 --platform 时可不指定）"),
    version: Optional[str] = typer.Argument(None,
                                            help="硬件所使用 MicroPython 固件版本，如 '1.20.0'（使用 --platform 时可不指定）"),
    variant: Optional[str] = typer.Option(None, "--variant", "-V",
                                          help="具体硬件变体，如 ESP32_GENERIC、PICO_W"),
    platform: Optional[str] = typer.Option(None, "--platform",
                                           help="串口号，如 COM3 或 /dev/ttyUSB0，自动检测硬件并下载对应 stubs"),
):
    """在已创建项目中初始化 MicroPython 环境"""
    init_stubs(hardware, version, variant, platform)

@app.command()
def config():
    """在当前目录创建默认 .pyrite_config.json"""
    create_default_config()


@app.command()
def board_info(
    port: str = typer.Argument(..., help="串口号，如 COM3 或 /dev/ttyUSB0"),
    baudrate: int = typer.Option(115200, "--baudrate", "-b", help="波特率"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="超时秒数"),
):
    """获取设备板级信息（固件、CPU、内存、Flash 等）"""
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
    mp = MicroPython(port=port, baudrate=baudrate, timeout=timeout)
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


def main():
    app()


if __name__ == "__main__":
    main()
