import time
from typing import Optional

import serial

from .transport import Transport


class SerialTransport(Transport):
    """基于 pyserial 的串口传输实现。"""

    def __init__(self, port: Optional[str] = None, baudrate: int = 115200, timeout: int = 10) -> None:
        super().__init__()
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._ser: Optional[serial.Serial] = None

    def connect(self) -> None:
        self.disconnect()
        if not self.port:
            raise ValueError("未提供串口号")
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=self.timeout,
            write_timeout=self.timeout,
        )
        time.sleep(0.3)
        self.reset_input_buffer()
        self.reset_output_buffer()

    def disconnect(self) -> None:
        if self._ser and self._ser.is_open:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None
        super().disconnect()

    def _raw_write(self, data: bytes) -> None:
        if self._ser is None:
            raise ConnectionError("串口未连接")
        self._ser.write(data)

    def _raw_read(self, size: int) -> bytes:
        if self._ser is None:
            raise ConnectionError("串口未连接")
        return self._ser.read(size)

    def _raw_in_waiting(self) -> int:
        if self._ser is None:
            return 0
        try:
            return self._ser.in_waiting
        except Exception:
            return 0

    def reset_output_buffer(self) -> None:
        if self._ser and self._ser.is_open:
            try:
                self._ser.reset_output_buffer()
            except Exception:
                pass

    @property
    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open
