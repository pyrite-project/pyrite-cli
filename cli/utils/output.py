import sys
import json as _json


def is_tty() -> bool:
    return sys.stdout.isatty()


def print_json(data) -> None:
    print(_json.dumps(data, ensure_ascii=False))


def log(msg: str = "", **kwargs) -> None:
    print(msg, file=sys.stderr, **kwargs)
