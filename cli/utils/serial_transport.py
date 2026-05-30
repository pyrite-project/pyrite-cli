"""
串口传输实现 — 基于 pyserial。

提供 SerialTransport 类，封装串口的打开、关闭、读写和
DTR/RTS 硬件复位信号控制。
"""

from __future__ import annotations

import time
from typing import Optional

import serial

from .log import get_logger
from .transport import Transport

log = get_logger(__name__)


class SerialTransport(Transport):
    """基于 pyserial 的串口传输实现。"""

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = 115200,
        timeout: int = 10,
    ) -> None:
        super().__init__()
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._ser: Optional[serial.Serial] = None

    def connect(self) -> None:
        self.disconnect()
        if not self.port:
            raise ValueError("未提供串口号")
        log.debug("打开串口 %s (波特率=%d, 超时=%ds)", self.port, self.baudrate, self.timeout)
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
            log.debug("关闭串口 %s", self.port)
            try:
                self._ser.close()
            except Exception as e:
                log.trace("关闭串口时忽略异常: %s", e)
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

    def set_dtr(self, state: bool) -> None:
        """设置 DTR 信号线状态。"""
        if self._ser and self._ser.is_open:
            self._ser.dtr = state

    def set_rts(self, state: bool) -> None:
        """设置 RTS 信号线状态。"""
        if self._ser and self._ser.is_open:
            self._ser.rts = state

    def dtr_rts_reset(self) -> None:
        """通过 DTR/RTS 信号线硬件复位设备。

        标准 ESP32/ESP8266 自动复位电路：
          RTS → EN  (RTS=高 → EN=低 → 芯片复位)
          DTR → GPIO0 (DTR=高 → GPIO0=低 → 下载模式)

        释放复位时必须保证 DTR=False (GPIO0=高)，否则芯片会进入下载模式。
        """
        if not self._ser or not self._ser.is_open:
            return
        log.trace("DTR/RTS 硬件复位")
        # 进入复位：RTS=高 → EN=低
        self._ser.rts = True
        self._ser.dtr = False  # GPIO0 保持高电平，确保正常启动
        time.sleep(0.1)
        # 释放复位：RTS=低 → EN=高，GPIO0 仍为高 → 芯片正常启动
        self._ser.rts = False
        time.sleep(0.5)
        self.reset_input_buffer()

    @property
    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open
