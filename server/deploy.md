# 部署步骤

## 服务器（宝塔）

### 1. 安装 Mosquitto（一次性）

```bash
bash setup.sh
```

Alibaba Cloud Linux 4 直接执行脚本即可，不需要手动安装 `epel-release`。

宝塔防火墙放行 `1883`。

### 2. 部署 Flask 应用（宝塔 Python 项目管理器）

1. 上传 `server/` 目录到 `/www/wwwroot/whitebox/`
2. 复制 `.env.example` → `.env`（本机 MQTT 就不用改）
   - `HOOK_STATE_STALE_SECONDS=1800` 表示 hook 工作态 30 分钟无新事件才兜底回 `idle`，避免长时间思考时误显示空闲。
3. 宝塔 → 网站 → Python 项目 → 添加：

| 字段 | 值 |
|------|-----|
| 项目路径 | `/www/wwwroot/whitebox` |
| 启动文件 | `app.py` |
| 端口 | `8080` |

4. 点「模块」安装 requirements.txt
5. 启动

### 3. 验证

```bash
curl http://127.0.0.1:8080/healthz
# {"ok":true,"mqtt":true,"ts":"..."}
```

验证 MQTT action 订阅（服务器需订阅 `whitebox/device/+/action`）：

```bash
# 发布一个测试 action
mosquitto_pub -t whitebox/device/whitebox-001/action \
  -m '{"action":"continue","source":"button","last_seq":1,"state":"marinating","device_id":"whitebox-001","ts":1}'

# 查看服务器状态
curl http://127.0.0.1:8080/api/state
# devices.whitebox-001.last_action 应包含上面的 action
```

## PC 端

### 启动 cc-dashboard

cc-dashboard 现在是常驻进程，包含：
- MQTT bridge（长连接发布状态 + 订阅 action）
- Hook HTTP API
- CC Controller
- Web 监控面板

```bash
cd cc-dashboard
pip install -r requirements.txt
python app.py
```

默认启动在 `http://localhost:5000`。

### 配置 `.env`

```ini
MQTT_HOST=your-mqtt-host
MQTT_PORT=1883
MQTT_USERNAME=whitebox
MQTT_PASSWORD=your_mqtt_password
TOPIC_PREFIX=whitebox
DASHBOARD_HOOK_URL=http://127.0.0.1:5000/api/hook/state
HOOK_HTTP_TOKEN=your_hook_token
```

- `MQTT_HOST`：远程 broker 地址，dashboard 的 MQTT bridge 会连接
- `DASHBOARD_HOOK_URL`：hook 脚本的 HTTP 目标，指向本机 dashboard

### Hook 脚本工作方式

Hook 脚本 (`hooks/whitebox_status_hook.py`) 现在只做 HTTP POST 到本机 dashboard：
1. 读取 stdin 事件
2. 构建 payload
3. POST 到 `DASHBOARD_HOOK_URL`
4. 成功 → 立即退出
5. 失败 → 写入 `.queue` 文件，立即退出

Dashboard 常驻进程负责：
- 通过 MQTT 长连接发布状态（低延迟，无每次连接开销）
- 后台 drainer 清理 `.queue` 中的缓存状态

### CC 控制 API

Dashboard 托管 Claude Code session：

```bash
# 启动托管 session
curl -X POST http://localhost:5000/api/cc/start \
  -H 'Content-Type: application/json' \
  -d '{"work_dir": "D:/manbo/white_box"}'

# 发送 prompt
curl -X POST http://localhost:5000/api/cc/send \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "列出当前目录文件"}'

# 查看 session 状态
curl http://localhost:5000/api/cc/state
```

设备 action 自动映射到 CC 控制：
- `marinating` + `continue` → 允许权限请求 / 确认问题
- `marinating` + `reject` → 拒绝权限请求 / 否认问题

## Topic

| Topic | 方向 | 说明 |
|-------|------|------|
| `whitebox/pc/state` | Dashboard→Broker→ESP32 | 状态（retained） |
| `whitebox/device/+/ack` | ESP32→Broker→Server | 确认回执 |
| `whitebox/device/+/availability` | ESP32→Broker→Server | 在线/离线 |
| `whitebox/device/+/action` | ESP32→Broker→Dashboard | 按钮/语音动作 |
