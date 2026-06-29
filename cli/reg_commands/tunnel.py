from __future__ import annotations

from typing import List, Optional

import typer

from ..utils.tunnel import (
    NetworkPolicy,
    TunnelError,
    run_keyboard_tunnel,
    run_network_tunnel,
)
from .common import DEFAULT_BAUDRATE, _complete_port, _mp_factory, log


tunnel_app = typer.Typer(
    help="主机能力透传：键盘输入与开发期 HTTP(S) 网络跳板",
    add_completion=False,
)


def register(app: typer.Typer) -> None:
    app.add_typer(tunnel_app, name="tunnel")


@tunnel_app.command("kb")
def tunnel_kb(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    baudrate: int = typer.Option(
        DEFAULT_BAUDRATE,
        "--baudrate",
        "-b",
        help="波特率",
        envvar="PYRITE_BAUDRATE",
    ),
    timeout: int = typer.Option(
        10,
        "--timeout",
        "-t",
        help="连接超时秒数",
        envvar="PYRITE_TIMEOUT",
    ),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
) -> None:
    """把上位机键盘事件透传给设备端 helper。"""
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        log.info("tunnel kb 已启动，按 Ctrl+C 或 Ctrl+] 退出")
        run_keyboard_tunnel(mp)
    except KeyboardInterrupt:
        log.info("用户中断 tunnel kb")
    except TunnelError as exc:
        log.error("%s", exc)
        raise typer.Exit(1) from exc
    finally:
        mp.disconnect()


@tunnel_app.command("network")
def tunnel_network(
    port: str = typer.Argument(..., help="串口号", autocompletion=_complete_port),
    allow: Optional[List[str]] = typer.Option(
        None,
        "--allow",
        "-a",
        help="允许访问的域名，可重复；example.com 同时允许其子域名",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="上位机侧标识，随握手暴露给设备端 helper",
    ),
    request_timeout: float = typer.Option(
        10.0,
        "--request-timeout",
        min=0.1,
        help="单次 HTTP(S) 请求超时秒数",
    ),
    max_response_bytes: int = typer.Option(
        64 * 1024,
        "--max-response-bytes",
        min=0,
        help="返回给设备端的最大响应体字节数",
    ),
    allow_private: bool = typer.Option(
        False,
        "--allow-private",
        help="允许访问 localhost、私有地址和链路本地地址",
    ),
    allow_webrepl: bool = typer.Option(
        False,
        "--allow-webrepl",
        help="允许在 WebREPL 连接上启动 network tunnel",
    ),
    baudrate: int = typer.Option(
        DEFAULT_BAUDRATE,
        "--baudrate",
        "-b",
        help="波特率",
        envvar="PYRITE_BAUDRATE",
    ),
    timeout: int = typer.Option(
        10,
        "--timeout",
        "-t",
        help="连接超时秒数",
        envvar="PYRITE_TIMEOUT",
    ),
    ws: Optional[str] = typer.Option(None, "--ws", help="WebREPL URL"),
    password: Optional[str] = typer.Option(None, "--password", help="WebREPL 密码"),
) -> None:
    """让设备端通过上位机发起受限 HTTP(S) 请求。"""
    if ws and not allow_webrepl:
        log.error(
            "tunnel network 默认不在 WebREPL 上启用；如确认需要，请增加 --allow-webrepl"
        )
        raise typer.Exit(2)

    _ = host
    policy = NetworkPolicy(
        allow_hosts=tuple(allow or ()),
        timeout=request_timeout,
        max_response_bytes=max_response_bytes,
        allow_private=allow_private,
    )
    mp = _mp_factory(port, baudrate, timeout, ws, password)
    try:
        mp.connect()
        log.info("tunnel network 已启动，按 Ctrl+C 退出")
        run_network_tunnel(mp, policy)
    except KeyboardInterrupt:
        log.info("用户中断 tunnel network")
    except TunnelError as exc:
        log.error("%s", exc)
        raise typer.Exit(1) from exc
    finally:
        mp.disconnect()
