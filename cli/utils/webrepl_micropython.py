from typing import Optional

from .flash import MicroPython
from .transport import Transport
from .webrepl_transport import WebREPLTransport


class WebREPLMicroPython(MicroPython):
    """通过 WebREPL (WebSocket) 与 MicroPython 设备交互。

    继承 MicroPython 的所有高级操作（刷入、文件管理、代码执行等），
    但底层使用 WebSocket WebREPL 传输，而非串口。
    """

    def __init__(
        self,
        url: str,
        password: Optional[str] = None,
        timeout: int = 10,
        transport: Optional['Transport'] = None,
    ) -> None:
        t = transport or WebREPLTransport(url, password)
        # 不传 port/baudrate，基类 __init__ 中 transport 优先
        # port 设为 url 用于连接标识
        super().__init__(port=url, timeout=timeout, transport=t)
        self.url = url

    def connect(self, port: Optional[str] = None, baudrate: Optional[int] = None) -> bool:
        """建立 WebREPL 连接（忽略串口特定的 port/baudrate 参数）。"""
        if self.is_connected:
            self.disconnect()
        self.transport.connect()
        return True
