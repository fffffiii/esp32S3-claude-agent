# esp-wifi-connect 配网组件使用指南

## 组件概述

`esp-wifi-connect` (v3.1.4) 是一个 ESP32 WiFi 配网组件，提供两种模式：

- **Station 模式**：自动连接已保存的 WiFi
- **AP 配网模式**：创建热点 + Captive Portal Web 页面，用户通过手机/电脑配网

## 核心文件说明

| 文件 | 作用 |
|------|------|
| `wifi_manager.cc/.h` | 顶层单例，管理 Station/AP 模式切换和事件回调 |
| `wifi_station.cc/.h` | Station 模式：扫描、连接、断线重连、BSSID 记忆 |
| `wifi_configuration_ap.cc/.h` | AP 模式：创建热点 + HTTP Web Server + Captive Portal |
| `ssid_manager.cc/.h` | NVS 存储管理：最多保存 10 组 SSID/密码 |
| `dns_server.cc/.h` | UDP DNS 服务器：拦截所有 DNS 请求返回 AP 网关 IP |

## 快速使用

### 1. 在 CMakeLists.txt 中添加依赖

```cmake
# main/CMakeLists.txt
idf_component_register(
    ...
    REQUIRES esp_wifi_connect
)

# 或在 idf_component.yml 中添加
dependencies:
  78/esp-wifi-connect:
    version: "~3.1.4"
```

### 2. 基本代码

```cpp
#include "wifi_manager.h"

// 初始化
auto& wifi = WifiManager::GetInstance();
WifiManagerConfig config;
config.ssid_prefix = "WhiteBox";     // 热点名称前缀，实际显示如 "WhiteBox-A1B2"
config.language = "zh-CN";           // Web UI 语言
config.station_scan_min_interval_seconds = 10;
config.station_scan_max_interval_seconds = 300;
wifi.Initialize(config);

// 注册事件回调
wifi.SetEventCallback([](WifiEvent event, const std::string& data) {
    switch (event) {
        case WifiEvent::Connected:
            printf("WiFi 已连接: %s\n", data.c_str());
            printf("IP: %s\n", WifiManager::GetInstance().GetIpAddress().c_str());
            break;
        case WifiEvent::Disconnected:
            printf("WiFi 断开\n");
            break;
        case WifiEvent::ConfigModeEnter:
            printf("进入配网模式, 热点: %s\n",
                   WifiManager::GetInstance().GetApSsid().c_str());
            break;
        case WifiEvent::ConfigModeExit:
            printf("退出配网模式\n");
            break;
    }
});

// 启动 Station 模式（自动尝试连接已保存的 WiFi）
wifi.StartStation();

// 如果需要手动进入配网模式：
// wifi.StartConfigAp();
// 用户连接热点后访问 http://192.168.4.1 即可配网
```

### 3. C 语言接口（在 main.c 中使用）

如果需要在 C 代码中使用，需要封装一个 C 接口：

```c
// wifi_connect_wrapper.h
#pragma once
#ifdef __cplusplus
extern "C" {
#endif

void wifi_connect_init(const char* ssid_prefix, const char* language);
void wifi_connect_start_station(void);
void wifi_connect_start_ap(void);
void wifi_connect_stop_ap(void);
bool wifi_connect_is_connected(void);
const char* wifi_connect_get_ip(void);
const char* wifi_connect_get_ssid(void);

#ifdef __cplusplus
}
#endif
```

## HTTP API 接口

组件在 AP 模式下启动 HTTP Server，提供以下 REST 接口：

### Web UI 页面

| URI | 方法 | 说明 |
|-----|------|------|
| `/` | GET | Captive Portal 主页面（WiFi 配置 UI） |
| `/done.html` | GET | 配置成功页面 |

### 配置接口

| URI | 方法 | 请求体 | 响应 | 说明 |
|-----|------|--------|------|------|
| `/scan` | GET | - | `{"support_5g":bool,"aps":[{"ssid":"..","rssi":-60,"authmode":4},...]}` | 扫描附近 WiFi |
| `/submit` | POST | `{"ssid":"..","password":".."}` | `{"success":true/false}` | 提交 WiFi 连接 |
| `/exit` | POST | - | `{"success":true}` | 退出配网模式 |
| `/saved/list` | GET | - | `["ssid1","ssid2",...]` | 获取已保存的 WiFi 列表 |
| `/saved/set_default` | GET | `?index=N` | - | 设置默认 WiFi（移到首位） |
| `/saved/delete` | GET | `?index=N` | - | 删除已保存的 WiFi |
| `/advanced/config` | GET | - | `{"ota_url":"..","max_tx_power":80,...}` | 获取高级配置 |
| `/advanced/submit` | POST | `{"ota_url":"..","max_tx_power":80,...}` | `{"success":true/false}` | 保存高级配置 |

### Captive Portal 重定向

以下 URL 会被 302 重定向到 `http://192.168.4.1/?lang=<语言>`，实现 iOS/Android/Windows 自动弹出配网页面：

- `/hotspot-detect.html` (iOS/macOS)
- `/generate_204`, `/gen_204` (Android)
- `/mobile/status.php` (Android 旧版)
- `/check_network_status.txt` (Android)
- `/ncsi.txt`, `/connecttest.txt` (Windows)
- `/fwlink/` (Windows 旧版)
- `/connectivity-check.html` (Firefox/Linux)
- `/success.txt` (Linux)
- `/portal.html`, `/library/test/success.html`

## UDP DNS 服务器

组件启动一个 UDP DNS 服务器（端口 53），将**所有 DNS 查询响应为 AP 网关 IP (192.168.4.1)**。这是 Captive Portal 的核心机制：

- 设备连接热点后，尝试解析任意域名
- DNS 服务器返回 192.168.4.1
- 浏览器访问 192.168.4.1，弹出配网页面
- 停止时通过 `shutdown()` + `close()` 优雅关闭

## WiFi 连接流程

1. 用户在 Web UI 选择 SSID 并输入密码
2. 前端 POST JSON 到 `/submit`
3. 后端尝试连接（2.4G 等待 10 秒，5G 等待 25 秒）
4. 成功：保存凭证到 NVS，返回 `{"success":true}`
5. 前端跳转到 `/done.html`
6. `/done.html` 自动 POST `/exit` 退出配网模式
7. 设备切换到 Station 模式正常连接

## NVS 存储

凭证存储在 NVS 的 `"wifi"` 命名空间下：

- `ssid` / `password` — 默认 WiFi
- `ssid1`-`ssid9` / `password1`-`password9` — 其他保存的 WiFi
- 最多 10 组

## 高级配置

| 配置项 | 说明 | 范围 |
|--------|------|------|
| `ota_url` | 自定义 OTA 升级地址 | 字符串 |
| `max_tx_power` | WiFi 最大发射功率 | 8(2dBm) ~ 80(20dBm) |
| `remember_bssid` | 连接时记住 BSSID | true/false |
| `sleep_mode` | 启用 WiFi 省电模式 | true/false |

## Web UI 自定义（白绿色配色）

HTML 文件位于 `managed_components/78__esp-wifi-connect/assets/`，通过 `EMBED_TXTFILES` 编译进固件。

### 备份位置

白绿色配色版本已备份到：
```
main/assets/wifi_connect_backup/wifi_configuration.html
main/assets/wifi_connect_backup/wifi_configuration_done.html
```

### 配色方案

| 元素 | 颜色 | 色值 |
|------|------|------|
| 背景 | 浅绿白 | `#f4f9f4` |
| 按钮 | 深绿 | `#2e7d32` |
| 按钮悬停 | 深绿 | `#1b5e20` |
| 链接 | 深绿 | `#2e7d32` |
| 边框 | 浅绿 | `#c8e6c9` |
| 选中 Tab 边框 | 绿 | `#43a047` |
| Toast 提示 | 绿底 | `rgba(46,125,50,0.9)` |
| 加载动画 | 绿色 | `#43a047` |

### 组件更新后恢复配色

如果 `idf.py reconfigure` 或组件管理器重新下载了组件，需要重新应用配色：

```bash
# 方法1：手动复制备份文件
cp main/assets/wifi_connect_backup/wifi_configuration.html \
   managed_components/78__esp-wifi-connect/assets/

cp main/assets/wifi_connect_backup/wifi_configuration_done.html \
   managed_components/78__esp-wifi-connect/assets/

# 方法2：在 CMakeLists.txt 中修改引用路径（需要自定义组件 CMakeLists）
# 将 EMBED_TXTFILES 路径指向 main/assets/wifi_connect_backup/
```

## 注意事项

1. **组件管理器覆盖**：`idf.py reconfigure` 会重新下载组件，覆盖所有修改。HTML 备份在 `main/` 目录下是安全的。
2. **WiFi 连接验证**：`/submit` 接口会先尝试连接 10-25 秒验证密码是否正确，超时返回失败。
3. **内存限制**：HTTP Server 配置 `max_uri_handlers = 24`，不要注册过多 URI。
4. **Body 大小限制**：`/submit` 最大接受 1024 字节的请求体。
5. **SSID 长度限制**：最大 32 字符，密码最大 64 字符。
6. **iOS 兼容**：HTML 包含 iOS `touchstart` workaround 解决 readonly input 无法聚焦的 bug。
