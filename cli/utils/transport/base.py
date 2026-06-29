from abc import ABC, abstractmethod


class Transport(ABC):
    """传输层抽象基类，支持 Serial 和 WebREPL 两种实现。"""

    _rx_buf: bytes

    def __init__(self) -> None:
        self._rx_buf = b""

    @abstractmethod
    def connect(self) -> None:
        """建立底层连接（串口打开 / WebSocket 握手）。"""
        ...

    def disconnect(self) -> None:
        """断开底层连接。"""
        self._rx_buf = b""

    @abstractmethod
    def _raw_write(self, data: bytes) -> None:
        """底层写入（子类实现）。"""
        ...

    @abstractmethod
    def _raw_read(self, size: int) -> bytes:
        """底层读取（子类实现），最多返回 size 字节。"""
        ...

    @abstractmethod
    def _raw_in_waiting(self) -> int:
        """底层接收缓冲区可用字节数。"""
        ...

    def write(self, data: bytes) -> None:
        """写入数据。"""
        self._raw_write(data)

    def read(self, size: int = -1) -> bytes:
        """读取数据。size=-1 表示读取所有可用数据。"""
        if not self._rx_buf:
            self._fill_buf()
        if size < 0 or size >= len(self._rx_buf):
            data = self._rx_buf
            self._rx_buf = b""
            return data
        data = self._rx_buf[:size]
        self._rx_buf = self._rx_buf[size:]
        return data

    @property
    def in_waiting(self) -> int:
        """接收缓冲区中的可用字节数。"""
        if not self._rx_buf:
            self._fill_buf()
        return len(self._rx_buf)

    def reset_input_buffer(self) -> None:
        """清空接收缓冲区。"""
        self._rx_buf = b""
        try:
            while self._raw_in_waiting() > 0:
                self._raw_read(self._raw_in_waiting())
        except Exception:
            pass

    def reset_output_buffer(self) -> None:
        """清空发送缓冲区（串口有效，WebSocket 为空操作）。"""
        pass

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """是否已连接。"""
        ...

    # ── 内部帮助方法 ──

    def _fill_buf(self) -> None:
        """从底层读取数据填充内部接收缓冲区。"""
        try:
            n = self._raw_in_waiting()
            if n > 0:
                chunk = self._raw_read(n)
                if chunk:
                    self._rx_buf += chunk
        except Exception:
            pass
