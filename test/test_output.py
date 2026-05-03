"""设备基础功能测试 - 输出系统信息并验证基本功能"""

import sys
import os
import math
import time
import gc


def test_system_info():
    """输出设备系统信息。"""
    print("--- 系统信息 ---")
    try:
        uname = os.uname()
        print(f"  系统:    {uname.sysname}")
        print(f"  节点:    {uname.nodename}")
        print(f"  版本:    {uname.release}")
        print(f"  固件:    {uname.version}")
        print(f"  机器:    {uname.machine}")
    except AttributeError:
        print(f"  平台:    {sys.platform}")
    print(f"  Python:  {sys.version}")
    print(f"  频率:    {machine.freq() // 1000000} MHz")
    print()


def test_memory():
    """检查内存使用情况。"""
    print("--- 内存信息 ---")
    gc.collect()
    free = gc.mem_free()
    alloc = gc.mem_alloc()
    print(f"  已用:    {alloc} 字节 ({alloc / (free + alloc) * 100:.1f}%)")
    print(f"  空闲:    {free} 字节")
    print(f"  总计:    {free + alloc} 字节")
    print()


def test_math():
    """测试基础数学运算。"""
    print("--- 数学运算测试 ---")
    tests = [
        ("1 + 1", 1 + 1, 2),
        ("10 * 3.14", 10 * 3.14, 31.4),
        ("math.sqrt(144)", math.sqrt(144), 12),
        ("math.sin(math.pi / 2)", round(math.sin(math.pi / 2), 1), 1.0),
        ("math.log(math.e)", round(math.log(math.e), 0), 1.0),
    ]
    for expr, result, expected in tests:
        status = "PASS" if abs(result - expected) < 0.01 else "FAIL"
        print(f"  [{status}] {expr} = {result}")
    print()


def test_string():
    """测试字符串操作。"""
    print("--- 字符串操作测试 ---")
    s = "MicroPython on Pyrite"
    tests = [
        ("len()", len(s), 21),
        ("upper()", s.upper(), "MICROPYTHON ON PYRITE"),
        ("startswith('M')", s.startswith("M"), True),
        ("split()", len(s.split()), 4),
        ("replace()", s.replace("Pyrite", "Device"), "MicroPython on Device"),
    ]
    for name, result, expected in tests:
        status = "PASS" if result == expected else "FAIL"
        print(f"  [{status}] {name} = {result}")
    print()


def test_list_dict():
    """测试列表和字典操作。"""
    print("--- 容器操作测试 ---")
    lst = [3, 1, 4, 1, 5, 9, 2, 6]
    lst.sort()
    assert lst == [1, 1, 2, 3, 4, 5, 6, 9], f"排序结果不正确: {lst}"
    print(f"  [PASS] 排序: {lst}")

    d = {"a": 1, "b": 2, "c": 3}
    assert d["a"] + d["c"] == 4, "字典取值错误"
    print(f"  [PASS] 字典: {d}")

    s = set([1, 2, 3, 2, 1])
    assert s == {1, 2, 3}, f"集合去重错误: {s}"
    print(f"  [PASS] 集合: {s}")
    print()


def test_led(blink_count=3):
    """测试板载 LED 闪烁（如果存在）。"""
    print("--- 板载 LED 测试 ---")
    import machine

    for pin_name in ["LED", "D4", "D0", 2, 16, "GPIO2"]:
        try:
            led = machine.Pin(pin_name, machine.Pin.OUT)
            for _ in range(blink_count):
                led.on()
                time.sleep_ms(150)
                led.off()
                time.sleep_ms(150)
            print(f"  [PASS] LED 在 {pin_name} 上 ({blink_count} 次闪烁)")
            return
        except Exception:
            continue
    print("  [跳过] 未检测到板载 LED")
    print()


def test_file_io():
    """测试文件读写操作。"""
    print("--- 文件系统测试 ---")

    content = "Hello from Pyrite!\nLine 2\nLine 3"
    try:
        with open("/tmp_test.txt", "w") as f:
            f.write(content)
        with open("/tmp_test.txt", "r") as f:
            readback = f.read()
        assert readback == content, "内容不匹配"
        os.remove("/tmp_test.txt")
        print("  [PASS] 文件写入/读取/删除成功")
    except Exception as e:
        print(f"  [FAIL] 文件操作失败: {e}")
    print()


def main():
    print()
    print("=" * 42)
    print("  MicroPython 设备功能测试")
    print("=" * 42)
    print()

    import machine

    test_system_info()
    test_memory()
    test_math()
    test_string()
    test_list_dict()
    test_file_io()
    test_led()

    print("=" * 42)
    print("  所有测试完成")
    print("=" * 42)
    print()


main()
