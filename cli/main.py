import argparse
from .utils.Flash import MicroPython, create_default_config


def add_serial_args(parser):
    parser.add_argument("port", help="串口号，如 COM3 或 /dev/ttyUSB0")
    parser.add_argument("-b", "--baudrate", type=int, default=115200, help="波特率（默认 115200）")
    parser.add_argument("-t", "--timeout", type=int, default=10, help="超时秒数（默认 10）")


def cmd_scan(args):
    ports = MicroPython.scan_ports()
    if not ports:
        print("未检测到串口设备。")
        return
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


def cmd_flash(args):
    mp = MicroPython(port=args.port, baudrate=args.baudrate, timeout=args.timeout)
    try:
        mp.connect()
        mp.flash_file(args.file, args.remote)
    finally:
        mp.disconnect()


def cmd_flash_program(args):
    mp = MicroPython(port=args.port, baudrate=args.baudrate, timeout=args.timeout)
    try:
        mp.connect()
        results = mp.flash_program(args.directory, args.prefix or "")
        ok = sum(1 for _, _, s in results if s)
        fail = sum(1 for _, _, s in results if not s)
        print(f"\n完成: {ok} 成功, {fail} 失败")
    finally:
        mp.disconnect()


def cmd_run(args):
    mp = MicroPython(port=args.port, baudrate=args.baudrate, timeout=args.timeout)
    try:
        mp.connect()
        output = mp.run(args.code)
        if output:
            print(output)
    finally:
        mp.disconnect()


def cmd_reset(args):
    mp = MicroPython(port=args.port, baudrate=args.baudrate)
    try:
        mp.connect()
        mp.reset()
        print("设备已重启。")
    finally:
        mp.disconnect()


def cmd_config(args):
    create_default_config()


def main():
    parser = argparse.ArgumentParser(
        description="## PYRITE-CLI ## - MicroPython 设备刷入工具",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    sub = parser.add_subparsers(title="命令", dest="command", required=True)

    p = sub.add_parser("scan", help="扫描所有可用串口设备")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("flash", help="刷入单个文件到设备")
    add_serial_args(p)
    p.add_argument("file", help="本地文件路径")
    p.add_argument("-r", "--remote", help="设备上的目标路径（默认使用文件名）")
    p.set_defaults(func=cmd_flash)

    p = sub.add_parser("flash-program", help="刷入整个目录到设备")
    add_serial_args(p)
    p.add_argument("directory", help="本地目录路径")
    p.add_argument("-p", "--prefix", help="设备上的远程路径前缀")
    p.set_defaults(func=cmd_flash_program)

    p = sub.add_parser("run", help="在设备上执行 Python 代码")
    add_serial_args(p)
    p.add_argument("code", help="要执行的 Python 代码")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("reset", help="软重启 MicroPython 设备")
    add_serial_args(p)
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("config", help="在当前目录创建默认 .pyrite_config.json")
    p.set_defaults(func=cmd_config)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
