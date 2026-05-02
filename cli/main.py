import typer
from typing import Optional
from .utils.Flash import MicroPython, create_default_config
from .project.project import init_project, init_stubs, new_project_interactive

app = typer.Typer(
    name="pyrite-cli",
    help="## PYRITE-CLI ## - MicroPython 设备刷入工具",
)


@app.command()
def scan(
    vid: Optional[int] = typer.Option(None, "--vid", help="按 VID 过滤（十进制）"),
    pid: Optional[int] = typer.Option(None, "--pid", help="按 PID 过滤（十进制）"),
    keyword: Optional[str] = typer.Option(None, "--keyword", "-k", help="按描述关键字过滤"),
    all: bool = typer.Option(False, "--all", "-a", help="显示所有设备（包括无 VID/PID 的）"),
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
):
    """刷入单个文件到设备"""
    mp = MicroPython(port=port, baudrate=baudrate, timeout=timeout)
    try:
        mp.connect()
        mp.flash_file(file, remote)
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
):
    """刷入整个目录到设备"""
    mp = MicroPython(port=port, baudrate=baudrate, timeout=timeout)
    try:
        mp.connect()
        results = mp.flash_program(directory, prefix or "")
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
    project_name: str = typer.Argument(..., help = "创建项目名称")
):
    '''创建新MicroPython项目'''
    new_project_interactive(project_name)

@app.command()
def init(
    hardware: str = typer.Argument(..., help = "使用的MicroPython名称"),
    version: str = typer.Argument(..., help = "硬件所使用MicroPython固件版本，形式同'1.20.0'"),
    variant: Optional[str] = typer.Option(None, "--variant", "-V",
                                          help = "具体硬件变体，如 ESP32_GENERIC、PICO_W")
):
    """在已创建项目中初始化MicroPython环境"""
    init_stubs(hardware, version, variant)

@app.command()
def config():
    """在当前目录创建默认 .pyrite_config.json"""
    create_default_config()


def main():
    app()


if __name__ == "__main__":
    main()
