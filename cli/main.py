import typer
from typing import Optional
from .utils.Flash import MicroPython, create_default_config

app = typer.Typer(
    name="pyrite-cli",
    help="## PYRITE-CLI ## - MicroPython 设备刷入工具",
)


@app.command()
def scan():
    """扫描所有可用串口设备"""
    ports = MicroPython.scan_ports()
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
def config():
    """在当前目录创建默认 .pyrite_config.json"""
    create_default_config()


def main():
    app()


if __name__ == "__main__":
    main()
