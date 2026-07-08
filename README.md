# Whitebox Delivery 交付目录说明

## 1. 目录用途

`delivery/` 是本次整理后的交付包，按实际职责拆成 3 个部分：

- `hardware/`：ESP32 固件源码、SPIFFS 资源、构建配置、资源处理脚本
- `device/`：运行在本地电脑上的 `cc-dashboard`，负责 Hook 接入、MQTT 桥接、Claude Code 控制
- `server/`：运行在远端服务器上的 Flask 服务，负责状态汇总、页面展示、历史记录、TTS 编排

本目录只保留交付所需的源码、配置样例和文档，不包含以下运行产物：

- `build/` 编译输出
- `__pycache__/`、`.pytest_cache/` 等缓存
- `*.db` 数据库文件
- `.queue`、`.trace.jsonl` 等运行时队列和日志缓存
- 实际使用中的 `.env` 敏感配置

## 2. 结构总览

```text
delivery/
├─ README.md
├─ hardware/
│  ├─ CMakeLists.txt
│  ├─ Makefile
│  ├─ dependencies.lock
│  ├─ partitions.csv
│  ├─ sdkconfig.defaults*
│  ├─ main/
│  ├─ scripts/
│  └─ spiffs/
├─ device/
│  ├─ .env.example
│  ├─ app.py
│  ├─ cc_controller.py
│  ├─ claude_stream.py
│  ├─ mqtt_bridge.py
│  ├─ mqtt_pub.py
│  ├─ tts_service.py
│  ├─ hooks/
│  ├─ templates/
│  └─ tests/
└─ server/
   ├─ .env.example
   ├─ app.py
   ├─ deploy.md
   ├─ requirements.txt
   ├─ setup.sh
   ├─ tts_service.py
   └─ templates/
```

## 3. 各端说明

### 3.1 硬件端

`hardware/` 是 ESP32 设备固件交付包，核心内容如下：

- `main/`
  - `main.c`：主流程入口，负责状态切换、GIF 调度、提示音播放
  - `whitebox_mqtt.c/.h`：MQTT 状态订阅、设备 action 上报
  - `whitebox_config.c/.h`：设备配置管理
  - `display.c/.h`：屏幕/GIF 显示逻辑
  - `audio_service.c/.h`、`speech_player.c/.h`、`task_sound.c/.h`：音频播放与提示音逻辑
  - `mqtt_integration_guide.md`：MQTT 接入说明
  - `wifi_connect_guide.md`：配网说明
- `spiffs/`
  - 设备运行时使用的 GIF、提示音资源
- `scripts/`
  - 素材转换、音频处理、SPIFFS 资源生成、发布辅助脚本
- `sdkconfig.defaults*`、`partitions*.csv`
  - 固件构建配置与分区配置

建议构建方式：

```bash
idf.py build
idf.py flash
idf.py monitor
```

### 3.2 设备端

`device/` 是本地电脑上的控制端，主要负责把 Claude Code 的状态和控制动作接进整个系统。

关键文件：

- `app.py`：本地 Flask 服务入口
- `mqtt_bridge.py`：常驻 MQTT 连接、收发桥接
- `cc_controller.py`：本地 Claude Code 会话控制
- `claude_stream.py`：Claude 输出流处理
- `hooks/whitebox_status_hook.py`：Claude Hook 上报脚本
- `tests/test_whitebox.py`：设备端测试
- `.env.example`：设备端配置样例

使用方式：

```bash
pip install -r requirements.txt
python app.py
```

部署前请先复制 `.env.example` 为 `.env`，再填入 MQTT、Hook Token、云端服务地址、TTS 配置。

### 3.3 服务端

`server/` 是远程部署的 Web + MQTT 服务。

关键文件：

- `app.py`：服务主入口，负责 MQTT 订阅、状态聚合、Web 页面、SSE 推送
- `tts_service.py`：TTS 编排与多 Provider 适配
- `setup.sh`：服务端初始化脚本
- `deploy.md`：部署说明
- `.env.example`：服务端配置样例

使用方式：

```bash
pip install -r requirements.txt
python app.py
```

如果走 Linux 服务器部署，优先参考 `deploy.md` 和 `setup.sh`。

## 4. 交付建议

- 交付源码时，直接以 `delivery/` 为主目录给对方即可
- 真正部署时，设备端和服务端都应从 `.env.example` 复制出自己的 `.env`
- 如果需要同时交付可烧录固件，可额外从原仓库 `releases/` 目录补充最新 `.bin` 文件
- 如果需要补充整体架构背景，可同时附上仓库根目录的 `README.md`
