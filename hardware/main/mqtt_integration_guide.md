# 硬件端 MQTT 集成指南

## 架构概览

```
┌─────────────┐     MQTT      ┌──────────────┐     HTTP     ┌──────────────┐
│  ESP32-S3   │◄────────────►│  Mosquitto   │◄────────────│ cc-dashboard │
│  (whitebox) │  subscribe:   │  (broker)    │             │  (常驻进程)   │
│             │  pc/state     │              │  publish:   │              │
│  WiFi STA   │  publish:     │              │  pc/state   │  MQTT Bridge │
│  Display    │  ack, avail,  │              │  subscribe: │  CC Controller│
│  Button     │  action       │              │  action     │  Hook API    │
│  Voice      │               └──────────────┘             └──────────────┘
└─────────────┘
```

## 新增文件

| 文件 | 说明 |
|------|------|
| `whitebox_config.h/.c` | NVS 配置读写（MQTT host/port/user/pass/topic_prefix/device_id） |
| `wifi_wrapper.h/.cc` | WifiManager C++ → C 封装层 |
| `whitebox_mqtt.h/.c` | ESP-MQTT 客户端：状态订阅/发布/action 发布 |

## 数据流

### PC → 设备（状态上报）

```
Claude Code Hook → HTTP POST 本机 dashboard
    → dashboard 常驻 MQTT client 发布 → whitebox/pc/state (retained, QoS 1)
    │
    ▼ (ESP32 收到)
JSON 解析 → status, seq
    │
    ├─ ESP_LOGI 打印 status / seq
    ├─ 回调 on_mqtt_state() → main.c 统一状态机切换 GIF
    └─ publish whitebox/device/whitebox-001/ack
```

### 设备 → PC（动作控制）

```
按钮/语音触发 → main.c 状态判断
    → whitebox_mqtt_publish_action("continue"/"reject", "button"/"voice")
    → MQTT publish → whitebox/device/{id}/action
    → dashboard MQTT bridge 订阅
    → PermissionRequest hook 等待 action
    → 需要托管 session 时再走 CC Controller
```

## MQTT Topic 约定

假设 `topic_prefix=whitebox`, `device_id=whitebox-001`：

| Topic | 方向 | QoS | Retain | 说明 |
|-------|------|-----|--------|------|
| `whitebox/pc/state` | PC→设备 | 1 | Yes | 状态 JSON |
| `whitebox/device/whitebox-001/ack` | 设备→PC | 1 | No | 收到 state 的回执 |
| `whitebox/device/whitebox-001/availability` | 设备→PC | 1 | Yes | `online` / `offline` |
| `whitebox/device/whitebox-001/action` | 设备→PC | 1 | No | 按钮/语音动作 |

## State Payload (PC→设备)

```json
{
  "status": "marinating",
  "gif": 4,
  "project_key": "my-project",
  "work_dir": "/home/user/project",
  "session_id": "abc-123",
  "msg_count": 5,
  "latest_message": {
    "role": "hook",
    "type": "PermissionRequest",
    "text": "[Bash] 等待确认",
    "ts": "..."
  },
  "updated_at": "2026-05-10T12:00:00Z",
  "seq": 42
}
```

## Status → GIF 映射

| status | GIF | 说明 |
|--------|-----|------|
| `cooking` | `c.gif` | 固定显示 |
| `thinking` | `b.gif` | 固定显示 |
| `marinating` | `e.gif` | 固定显示 |
| `idle` | `f/g/i/j` 随机 | 每 10 秒轮播 |
| `offline` | `f/g/i/j` 随机 | 同 idle |

注意：`gif` 字段保留日志兼容，设备端不再使用此字段决定显示内容，由 `status` 字段驱动。

## Action Payload (设备→PC)

```json
{
  "action": "continue",
  "source": "button",
  "last_seq": 42,
  "state": "marinating",
  "device_id": "whitebox-001",
  "ts": 1715400005
}
```

## 按钮规则

| 当前状态 | 单击 BOOT | 长按 BOOT |
|---------|----------|----------|
| `marinating` | 发布 `continue/source=button` | 发布 `reject/source=button` |
| `cooking` | 无动作 | 发布 `reject/source=button` |
| `thinking` | 无动作 | 发布 `reject/source=button` |
| `idle/offline/unknown` | 无动作 | 显示 `h.gif`，进入 AP 配网 |

## 语音规则

| 命令词 | 动作 | 生效状态 |
|--------|------|---------|
| `继续` / `确定` | `continue/source=voice` | 仅 `marinating` |
| `拒绝` | `reject/source=voice` | 仅 `marinating` |
| `小可` | 无动作（仅日志） | 所有状态 |

## 配网流程

1. **长按 BOOT 按钮（1.5秒以上）** → 停止 idle timer → 显示 `h.gif` → 停止 MQTT → 进入 AP 配网模式
2. 手机连接热点 `WhiteBox-XXXX`，自动弹出配网页面（或手动访问 `192.168.4.1`）
3. 在 **Wi-Fi Config** 选项卡选择 WiFi 并输入密码
4. 在 **Advanced** 选项卡向下滚动到 **MQTT Configuration** 部分：
   - MQTT Host：broker 地址，如 `192.168.1.100`
   - MQTT Port：默认 `1883`
   - MQTT Username / Password：broker 认证
   - Topic Prefix：默认 `whitebox`
   - Device ID：默认 `whitebox-001`
5. 点击 **Save** 保存 WiFi 连接成功后，再点 Advanced 的 **Save** 保存 MQTT 配置
6. 设备自动切换到 Station 模式并连接 MQTT

## NVS 存储

MQTT 配置存储在 NVS namespace `whitebox`：

| Key | 类型 | 默认值 |
|-----|------|--------|
| `mqtt_host` | string | `""` (空=不启动MQTT) |
| `mqtt_port` | u16 | `1883` |
| `mqtt_user` | string | `""` |
| `mqtt_pass` | string | `""` |
| `topic_pfx` | string | `whitebox` |
| `device_id` | string | `whitebox-001` |

## 启动流程

```
app_main()
  ├─ nvs_flash_init()
  ├─ wifi_wrapper_init()      // WiFi 初始化
  │   └─ wifi_wrapper_start_station()
  ├─ display_init()           // LCD + GIF + BOOT 按钮（不注册业务回调）
  ├─ whitebox_mqtt_set_state_cb(on_mqtt_state)  // 注册状态回调
  ├─ 注册按钮回调
  │   ├─ BOOT 单击 → on_boot_single_click
  │   └─ BOOT 长按 → on_boot_long_press
  ├─ audio_service_init(on_voice_action)  // 语音 → 动作回调
  └─ main loop
```

## 状态机

```
                    ┌──────────────────────────────────────────┐
                    │            main.c 状态机                  │
                    │                                          │
MQTT pc/state ────► │  s_current_state                         │
                    │    idle/offline → start idle GIF timer    │
                    │    cooking     → stop timer, c.gif       │
                    │    thinking    → stop timer, b.gif       │
                    │    marinating  → stop timer, e.gif       │
                    │                                          │
BOOT 单击 ────────► │  marinating → publish continue/button    │
                    │  其它 → 忽略                              │
                    │                                          │
BOOT 长按 ────────► │  marinating/cooking/thinking → reject    │
                    │  idle → h.gif + AP 配网                   │
                    │                                          │
语音 "继续/确定" ──►│  marinating → publish continue/voice     │
                    │  其它 → 只打日志                           │
                    │                                          │
语音 "拒绝" ───────►│  marinating → publish reject/voice       │
                    │  其它 → 只打日志                           │
                    └──────────────────────────────────────────┘
```

## 用 mosquitto 模拟测试

```bash
# 发布状态 → 设备应显示 c.gif
mosquitto_pub -t whitebox/pc/state -q 1 -r -m '{"status":"cooking","seq":1}'

# 发布状态 → 设备应显示 b.gif
mosquitto_pub -t whitebox/pc/state -q 1 -r -m '{"status":"thinking","seq":2}'

# 发布状态 → 设备应显示 e.gif
mosquitto_pub -t whitebox/pc/state -q 1 -r -m '{"status":"marinating","seq":3}'

# 发布状态 → 设备应开始 f/g/i/j 轮播
mosquitto_pub -t whitebox/pc/state -q 1 -r -m '{"status":"idle","seq":4}'

# 订阅 action → 设备按钮/语音操作时收到
mosquitto_sub -t "whitebox/device/+/action"
```

## 注意事项

1. **组件管理器覆盖**：修改 `wifi_configuration_ap.cc` 和 HTML 文件后，如果执行 `idf.py reconfigure` 会被覆盖。需要重新应用修改。
2. **MQTT Host 为空时不启动**：如果未配置 MQTT host，设备只做 WiFi STA + BLE + 语音，不会报错。
3. **断线重连**：WiFi 断开时自动停止 MQTT，重连后自动重启 MQTT。
4. **LWT**：设备异常断开（掉电/网络断），broker 自动发布 `availability = offline` (retained)。
5. **GIF 不再手动切换**：BOOT 单击/双击不再切换 GIF，所有 GIF 显示由 MQTT 状态驱动。
6. **Action topic 新增**：设备端发布 action 到 `{prefix}/device/{id}/action`，需要 dashboard 订阅处理。
