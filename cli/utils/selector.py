"""Cross-platform interactive terminal selector using keyboard navigation."""

import sys
import platform
import unicodedata


def _display_width(s: str) -> int:
    """返回字符串在终端中的显示列宽。CJK（全角）字符占 2 列。"""
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        for ch in s
    )


def _get_key() -> str | None:
    """Read a single keypress and return a normalized key name."""
    system = platform.system()

    if system == "Windows":
        import msvcrt

        key = msvcrt.getch()
        if key == b'\xe0':
            key = msvcrt.getch()
            if key == b'H':
                return "up"
            if key == b'P':
                return "down"
            return None
        if key == b'\r':
            return "enter"
        if key == b'\x03':
            raise KeyboardInterrupt
        return None

    import tty
    import termios

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == '\x1b':
            seq = sys.stdin.read(2)
            if seq == '[A':
                return "up"
            if seq == '[B':
                return "down"
            return None
        if ch == '\r':
            return "enter"
        if ch == '\x03':
            raise KeyboardInterrupt
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return None


def interactive_select(options: list[str], title: str = "请选择:") -> str:
    """Display an interactive selection list with keyboard navigation.

    Args:
        options: List of option strings to choose from.
        title: Prompt message displayed above the list.

    Returns:
        Selected option string.

    Exits the process with code 1 on Ctrl+C or when options is empty.
    """
    if not options:
        print(f"\n  {title}\n  (无可用选项)")
        sys.exit(1)

    if len(options) == 1:
        print(f"\n  {title}  \033[1m{options[0]}\033[0m (唯一选项)\n")
        return options[0]

    idx = 0
    n = len(options)
    max_w = max((_display_width(opt) for opt in options), default=20)
    title_dw = _display_width(title)
    box_w = min(max(max_w + 4, title_dw + 4, 30), 80)
    # 总行数：上框 + 标题 + 中框 + n个选项 + 下框 + 提示
    h = n + 5

    def _pad(text: str, target_dw: int) -> str:
        """右填充空格至目标显示宽度。"""
        cur = _display_width(text)
        return text + " " * max(0, target_dw - cur)

    def _emit():
        print(f"  \033[36m┌{'─' * box_w}┐\033[0m")
        print(f"  \033[36m│\033[0m {_pad(title, box_w - 2)} \033[36m│\033[0m")
        print(f"  \033[36m├{'─' * box_w}┤\033[0m")
        for i, opt in enumerate(options):
            visible = f"> {opt}" if i == idx else f"  {opt}"
            line = f" {_pad(visible, box_w - 2)} "
            if i == idx:
                sys.stdout.write(f"  \033[36m│\033[0m\033[7m{line}\033[0m\033[36m│\033[0m\n")
            else:
                sys.stdout.write(f"  \033[36m│\033[0m{line}\033[36m│\033[0m\n")
        sys.stdout.write(f"  \033[36m└{'─' * box_w}┘\033[0m\n")
        sys.stdout.write("  \033[2m(↑/↓)  (Enter)  (Ctrl+C)\033[0m\n")
        sys.stdout.flush()

    _emit()

    try:
        while True:
            key = _get_key()
            if key == "up" and idx > 0:
                idx -= 1
            elif key == "down" and idx < n - 1:
                idx += 1
            elif key == "enter":
                sys.stdout.write(f"\033[{h}A\033[J")
                print(f"  \033[1m{options[idx]}\033[0m")
                sys.stdout.flush()
                return options[idx]
            else:
                continue

            sys.stdout.write(f"\033[{h}A")
            _emit()
    except KeyboardInterrupt:
        sys.stdout.write(f"\033[{h}A\033[J")
        sys.exit(1)
