# manifest.py - 控制 flash-program 刷入哪些文件
#
# module(filename, remote=None, features=None)
#   刷入单个文件。remote 为设备上的目标路径（默认同 filename）。
#   features 列表: 任一个在 active_tags 中即刷入；None 表示无条件。
#
# package(dirname, remote=None, features=None)
#   递归刷入目录下所有 .py 文件。
#   支持 remote 前缀和 features 过滤，同 module。
#
# 条件编译 tags（详见 docs/条件编译与宏预处理.md）:
#   硬件 target: ESP32 / RP2040 / ESP8266 / STM32 / PICO
#   功能 feature: wifi / ble


# ── 基础功能测试（无条件刷入） ──
module("test_output.py", remote="test/test_output.py")
module("feature_stub.pyi", remote="dot/feature_stub.pyi")


# ── WiFi 测试（仅 wifi 设备刷入） ──
module("test_wifi.py", remote="test/test_wifi.py", features=["wifi"])
# 也可简写为:
# module("test_wifi.py", remote="test/test_wifi.py", features=["wifi"])


# ── 批量刷入整个 test 目录 ──
# 取消注释下一行以刷入目录下所有 .py:
# package(".")


# ══════════════════════════════════════════════════════════════════
# 进阶用法示例（取消注释即可使用）
# ══════════════════════════════════════════════════════════════════

# ── 按硬件平台条件刷入 ──
# module("drivers/esp_now.py", features=["ESP32"])
# module("drivers/rp2_pio.py", features=["RP2040"])

# ── 多个 feature 任意匹配 ──
# module("services/ota.py",   features=["wifi", "ble"])
# module("services/led.py",   features=["wifi", "ble"])

# ── 整个目录条件刷入 ──
# package("drivers/sensors", features=["wifi"])
# package("lib", remote="lib")
