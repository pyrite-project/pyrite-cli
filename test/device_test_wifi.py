"""WiFi 功能测试 - 扫描可用网络并测试连接"""

import network
import time

# ===== 配置 =====
# 如需测试连接，在此填写凭据
TEST_SSID = ""
TEST_PASSWORD = ""


def scan_wifi():
    """扫描并列出附近的 WiFi 网络。"""
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    time.sleep_ms(200)

    print("正在扫描 WiFi 网络...")
    nets = wlan.scan()
    print(f"找到 {len(nets)} 个网络:\n")

    # 按信号强度排序（从强到弱）
    nets.sort(key=lambda n: n[3], reverse=True)

    for i, (ssid, bssid, channel, rssi, auth_mode, hidden) in enumerate(nets, 1):
        ssid_str = ssid.decode("utf-8", errors="replace") if ssid else "(隐藏网络)"
        auth = ["开放", "WEP", "WPA-PSK", "WPA2-PSK", "WPA/WPA2"][auth_mode] if auth_mode < 5 else f"未知({auth_mode})"
        print(f"  {i:2d}. {ssid_str}")
        print(f"      信道: {channel}, 信号: {rssi} dBm, 加密: {auth}")
        if hidden:
            print("       (隐藏 SSID)")
        print()

    return wlan, nets


def test_connect(ssid, password):
    """尝试连接指定的 WiFi 网络。"""
    if not ssid:
        print("跳过连接测试 (未配置 SSID)")
        print("提示: 编辑文件顶部 TEST_SSID 和 TEST_PASSWORD 来测试连接")
        return False

    print(f"正在连接: {ssid}")
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if wlan.isconnected():
        wlan.disconnect()
        time.sleep_ms(500)

    wlan.connect(ssid, password)

    # 等待连接 (最多 15 秒)
    for _ in range(30):
        if wlan.isconnected():
            break
        time.sleep_ms(500)

    if wlan.isconnected():
        print(f"连接成功!")
        print(f"  IP: {wlan.ifconfig()[0]}")
        print(f"  子网掩码: {wlan.ifconfig()[1]}")
        print(f"  网关: {wlan.ifconfig()[2]}")
        print(f"  DNS: {wlan.ifconfig()[3]}")
        return True
    else:
        print(f"连接失败 (超时)")
        return False


def main():
    print("=" * 40)
    print("  WiFi 测试")
    print("=" * 40)

    wlan, nets = scan_wifi()

    if nets:
        test_connect(TEST_SSID, TEST_PASSWORD)
    else:
        print("未检测到 WiFi 适配器或硬件错误")

    print("\n测试完成")


main()
