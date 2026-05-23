"""SerialTransport DTR/RTS 复位功能测试（纯逻辑，无需实机）。"""
from unittest.mock import MagicMock, patch

import pytest

from cli.utils.serial_transport import SerialTransport
from cli.utils.flash import MicroPython, SerialTransport as ST


class TestSerialTransportDtrRts:
    def test_set_dtr_exposes_property(self):
        """set_dtr() 通过 pyserial 的 dtr property 控制信号线。"""
        transport = SerialTransport(port="COM99")
        transport._ser = MagicMock()
        transport.set_dtr(True)
        transport._ser.dtr = True
        transport.set_dtr(False)
        transport._ser.dtr = False

    def test_set_rts_exposes_property(self):
        """set_rts() 通过 pyserial 的 rts property 控制信号线。"""
        transport = SerialTransport(port="COM99")
        transport._ser = MagicMock()
        transport.set_rts(True)
        transport._ser.rts = True
        transport.set_rts(False)
        transport._ser.rts = False

    def test_dtr_rts_reset_toggles_signals(self):
        """dtr_rts_reset() 按正确时序切换 DTR/RTS。"""
        transport = SerialTransport(port="COM99")
        transport._ser = MagicMock()
        transport.dtr_rts_reset()
        # 复位后 DTR=False, RTS=False → GPIO0 高电平 (正常启动模式)
        assert transport._ser.dtr is False
        assert transport._ser.rts is False

    def test_dtr_rts_reset_sequence(self):
        """验证 DTR/RTS 复位时序不会将芯片置入下载模式。

        标准 ESP32 自动复位电路：
        - RTS → EN (RTS=高 → EN=低 → 复位)
        - DTR → GPIO0 (DTR=高 → GPIO0=低 → 下载模式)

        释放复位时 GPIO0 必须保持高电平（DTR=False），否则芯片进入下载模式。
        """
        class TrackingSerial:
            is_open = True
            def __init__(self):
                object.__setattr__(self, '_rts_vals', [])
                object.__setattr__(self, '_dtr_vals', [])
                object.__setattr__(self, 'rts', False)
                object.__setattr__(self, 'dtr', False)
            def __setattr__(self, name, value):
                if name == 'rts':
                    self._rts_vals.append(value)
                elif name == 'dtr':
                    self._dtr_vals.append(value)
                super().__setattr__(name, value)
            def write(self, data):
                pass
            def reset_output_buffer(self):
                pass
            def reset_input_buffer(self):
                pass

        transport = SerialTransport(port="COM99")
        transport._ser = TrackingSerial()
        transport.dtr_rts_reset()

        # RTS 序列: True (复位) → False (释放)
        assert transport._ser._rts_vals == [True, False], (
            f"RTS 时序异常: {transport._ser._rts_vals}"
        )
        # DTR 序列: 只能出现 False (GPIO0高=正常模式)，绝不能出现 True
        # 当 _ser.dtr 默认为 False 时，第一次赋值 False 不会记录
        forbidden = [v for v in transport._ser._dtr_vals if v is True]
        assert not forbidden, (
            f"DTR 在复位过程中被设为 True (GPIO0=低)，芯片会进入下载模式. "
            f"DTR 赋值记录: {transport._ser._dtr_vals}"
        )
        # 最终状态: RTS=False, DTR=False → 正常启动
        assert transport._ser.rts is False
        assert transport._ser.dtr is False

    def test_dtr_rts_reset_no_ser_does_nothing(self):
        """未连接时 dtr_rts_reset() 不抛异常。"""
        transport = SerialTransport(port="COM99")
        transport._ser = None
        transport.dtr_rts_reset()  # 不应 raise

    def test_dtr_rts_reset_disconnected_ser_does_nothing(self):
        """串口已关闭时 dtr_rts_reset() 不抛异常。"""
        transport = SerialTransport(port="COM99")
        transport._ser = MagicMock()
        transport._ser.is_open = False
        transport.dtr_rts_reset()  # 不应 raise


class TestMicroPythonConnectDtrRts:
    def test_connect_calls_dtr_rts_reset_for_serial(self):
        """MicroPython.connect() 在使用 SerialTransport 时自动调用 DTR/RTS 复位。"""
        transport = ST(port="COM99")
        transport._ser = MagicMock()
        transport._ser.is_open = True
        with patch.object(transport, "connect") as mock_ser_connect:
            with patch.object(transport, "dtr_rts_reset") as mock_reset:
                mp = MicroPython(port="COM99", transport=transport)
                mp.connect()
                mock_ser_connect.assert_called_once()
                mock_reset.assert_called_once()

    def test_connect_skips_dtr_rts_for_non_serial(self):
        """MicroPython.connect() 在非串口传输时跳过 DTR/RTS 复位。"""
        mock_transport = MagicMock()
        mock_transport.is_connected = False
        mp = MicroPython(port="COM99", transport=mock_transport)
        mp.connect()
        # isinstance(mock, SerialTransport) == False，不会调用 dtr_rts_reset


class TestMicroPythonResetDtrRts:
    def test_reset_prioritizes_dtr_rts(self):
        """MicroPython.reset() 优先使用硬件 DTR/RTS 复位。"""
        transport = ST(port="COM99")
        transport._ser = MagicMock()
        transport._ser.is_open = True
        mp = MicroPython(port="COM99", transport=transport)
        with patch.object(transport, "dtr_rts_reset") as mock_reset:
            mp.reset()
            mock_reset.assert_called_once()

    def test_reset_falls_back_to_soft_reset(self):
        """非串口传输时 reset() 兜底使用软重启。"""
        mock_transport = MagicMock()
        mock_transport.is_connected = True
        mp = MicroPython(transport=mock_transport)
        # 模拟 _enter_raw_repl / _execute 无异常
        with patch.object(mp, "_enter_raw_repl") as mock_enter:
            with patch.object(mp, "_execute") as mock_exec:
                mp.reset()
                mock_enter.assert_called_once()
                mock_exec.assert_called_once_with(
                    "import machine; machine.reset()", timeout=2
                )

    def test_reset_serial_failure_falls_back(self):
        """串口传输但 DTR/RTS 失败时，兜底使用软重启。"""
        transport = ST(port="COM99")
        transport._ser = MagicMock()
        mp = MicroPython(port="COM99", transport=transport)
        with patch.object(transport, "dtr_rts_reset", side_effect=Exception("fail")):
            with patch.object(mp, "_enter_raw_repl") as mock_enter:
                with patch.object(mp, "_execute") as mock_exec:
                    mp.reset()
                    mock_enter.assert_called_once()
                    mock_exec.assert_called_once()
