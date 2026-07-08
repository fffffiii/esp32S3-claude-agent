#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_random.h"
#include "nvs_flash.h"
#include "driver/gpio.h"
#include "display.h"
#include "audio_service.h"
#include "task_sound.h"
#include "speech_player.h"
#include "wifi_wrapper.h"
#include "whitebox_config.h"
#include "whitebox_mqtt.h"
#include "iot_button.h"

static const char *TAG = "MAIN";

/* ── State ── */
static volatile bool s_ap_mode_active = false;
static volatile bool s_wifi_connected = false;
static whitebox_mqtt_state_t s_current_state = WB_MQTT_STATE_UNKNOWN;
static whitebox_mqtt_state_t s_prev_state = WB_MQTT_STATE_UNKNOWN;
static esp_timer_handle_t s_idle_gif_timer = NULL;
static uint8_t s_last_idle_gif_index = 0xFF; /* 避免连续重复 */
static bool s_task_sound_ready = false;
static bool s_speech_player_ready = false;
static uint32_t s_last_done_seq = 0;
static uint32_t s_last_wait_seq = 0;
static char s_last_speech_id[40] = {0};

/* ── Idle 随机轮播 GIF 列表: f, g, i, j ── */
static const char s_idle_gif_chars[] = {'f', 'g', 'i', 'j'};
#define IDLE_GIF_COUNT (sizeof(s_idle_gif_chars))

static char pick_random_idle_gif(void)
{
    uint8_t idx;
    do {
        idx = (uint8_t)(esp_random() % IDLE_GIF_COUNT);
    } while (idx == s_last_idle_gif_index && IDLE_GIF_COUNT > 1);
    s_last_idle_gif_index = idx;
    return s_idle_gif_chars[idx];
}

/* ── Idle GIF timer 回调：每 10 秒随机切换 ── */
static void idle_gif_timer_cb(void *arg)
{
    (void)arg;
    char ch = pick_random_idle_gif();
    ESP_LOGI(TAG, "Idle GIF rotation -> %c", ch);
    display_switch_gif_by_char(ch);
}

static void start_idle_gif_timer(void)
{
    if (s_idle_gif_timer == NULL) {
        const esp_timer_create_args_t timer_args = {
            .callback = idle_gif_timer_cb,
            .name = "idle_gif",
        };
        esp_timer_create(&timer_args, &s_idle_gif_timer);
    }
    /* 首次立即随机显示一张 */
    char ch = pick_random_idle_gif();
    ESP_LOGI(TAG, "Entering idle, showing %c", ch);
    display_switch_gif_by_char(ch);
    /* 启动 10s 周期 timer（如果已运行则先停止再启动） */
    esp_timer_stop(s_idle_gif_timer);
    esp_timer_start_periodic(s_idle_gif_timer, 10 * 1000000ULL);
}

static void stop_idle_gif_timer(void)
{
    if (s_idle_gif_timer) {
        esp_timer_stop(s_idle_gif_timer);
    }
}

static void play_speech_if_new(const char *speech_id,
                               const char *audio_url,
                               const char *text,
                               uint32_t sample_rate,
                               uint32_t seq)
{
    if (!s_speech_player_ready ||
        !speech_id || speech_id[0] == '\0' ||
        !audio_url || audio_url[0] == '\0' ||
        strcmp(speech_id, s_last_speech_id) == 0) {
        return;
    }

    speech_player_request_t req = {0};
    strlcpy(req.speech_id, speech_id, sizeof(req.speech_id));
    strlcpy(req.audio_url, audio_url, sizeof(req.audio_url));
    if (text) {
        strlcpy(req.text, text, sizeof(req.text));
    }
    req.sample_rate = sample_rate ? sample_rate : 24000;

    ESP_LOGI(TAG, "Play speech id=%s seq=%lu", req.speech_id, (unsigned long)seq);
    task_sound_stop();
    if (speech_player_play(&req) == ESP_OK) {
        strlcpy(s_last_speech_id, req.speech_id, sizeof(s_last_speech_id));
    } else {
        ESP_LOGW(TAG, "Speech play request failed");
    }
}

/* ── MQTT 状态回调：统一调度 GIF + 提示音 ── */
static void on_mqtt_state(const whitebox_mqtt_state_event_t *event)
{
    ESP_LOGI(TAG, "State change: %s (seq=%lu hook=%s)",
             event->status, (unsigned long)event->seq, event->hook_event_name);
    whitebox_mqtt_state_t prev_state = s_current_state;
    bool state_changed = prev_state != event->state;
    s_prev_state = prev_state;
    s_current_state = event->state;

    if (state_changed) {
        switch (event->state) {
            case WB_MQTT_STATE_IDLE:
            case WB_MQTT_STATE_OFFLINE:
                start_idle_gif_timer();
                break;

            case WB_MQTT_STATE_COOKING:
                stop_idle_gif_timer();
                display_switch_gif_by_char('c');
                break;

            case WB_MQTT_STATE_THINKING:
                stop_idle_gif_timer();
                display_switch_gif_by_char('b');
                break;

            case WB_MQTT_STATE_MARINATING:
                stop_idle_gif_timer();
                display_switch_gif_by_char('e');
                break;

            default:
                ESP_LOGW(TAG, "Unknown state, ignoring");
                break;
        }
    } else {
        ESP_LOGD(TAG, "State unchanged, skip GIF switch: %s", event->status);
    }

    /* ── 提示音触发 ── */
    if (!s_task_sound_ready && !s_speech_player_ready) return;

    /* 完成音: 从 thinking/cooking/marinating → idle，且 hook_event_name == "Stop" */
    if (s_task_sound_ready &&
        event->state == WB_MQTT_STATE_IDLE &&
        (s_prev_state == WB_MQTT_STATE_THINKING ||
         s_prev_state == WB_MQTT_STATE_COOKING ||
         s_prev_state == WB_MQTT_STATE_MARINATING) &&
        strcmp(event->hook_event_name, "Stop") == 0 &&
        event->seq != s_last_done_seq) {
        s_last_done_seq = event->seq;
        ESP_LOGI(TAG, "Play task_done sound (seq=%lu)", (unsigned long)event->seq);
        speech_player_stop();
        task_sound_play(TASK_SOUND_DONE);
    }

    /* 等待确认音: 进入 marinating 且 hook 是 PermissionRequest 或等待类通知 */
    if (s_task_sound_ready &&
        event->state == WB_MQTT_STATE_MARINATING &&
        (strcmp(event->hook_event_name, "PermissionRequest") == 0 ||
         strcmp(event->hook_event_name, "Notification") == 0) &&
        event->seq != s_last_wait_seq) {
        s_last_wait_seq = event->seq;
        ESP_LOGI(TAG, "Play permission_wait sound (seq=%lu hook=%s)",
                 (unsigned long)event->seq, event->hook_event_name);
        speech_player_stop();
        task_sound_play(TASK_SOUND_PERMISSION_WAIT);
    }

    /* TTS 进度朗读: 只播放新的 speech_id，避免 retained 状态重复播 */
    if (s_speech_player_ready &&
        event->speech_id[0] != '\0' &&
        event->speech_audio_url[0] != '\0' &&
        strcmp(event->speech_id, s_last_speech_id) != 0) {
        play_speech_if_new(event->speech_id,
                           event->speech_audio_url,
                           event->speech_text,
                           event->speech_sample_rate,
                           event->seq);
    }
}

static void on_mqtt_speech(const whitebox_mqtt_speech_event_t *event)
{
    ESP_LOGI(TAG, "Speech ready: id=%s seq=%lu status=%s",
             event->speech_id, (unsigned long)event->seq, event->status);
    play_speech_if_new(event->speech_id,
                       event->speech_audio_url,
                       event->speech_text,
                       event->speech_sample_rate,
                       event->seq);
}

/* ── 语音 action 回调 ── */
static const char *map_voice_action(const char *zh)
{
    if (strcmp(zh, "继续") == 0 || strcmp(zh, "确定") == 0)
        return "continue";
    if (strcmp(zh, "拒绝") == 0)
        return "reject";
    return zh;
}

static void save_volume_to_nvs(int volume)
{
    whitebox_config_t cfg;
    if (whitebox_config_load(&cfg) == ESP_OK) {
        cfg.volume = volume;
        whitebox_config_save(&cfg);
        ESP_LOGI(TAG, "Volume %d saved to NVS", volume);
    }
}

static void on_mqtt_volume(const whitebox_mqtt_volume_event_t *ev)
{
    ESP_LOGI(TAG, "MQTT volume command: %d", ev->volume);
    audio_service_set_volume(ev->volume);
}

static void on_voice_action(const char *action)
{
    ESP_LOGI(TAG, "Voice action: %s (state=%d)", action, s_current_state);

    /* 音量命令: 任何状态下都生效 */
    if (strcmp(action, "调大音量") == 0) {
        int vol = audio_service_get_volume() + 10;
        if (vol > 100) vol = 100;
        audio_service_set_volume(vol);
        save_volume_to_nvs(vol);
        if (s_task_sound_ready) {
            speech_player_stop();
            task_sound_play(TASK_SOUND_ACTION_CONFIRM);
        }
        return;
    }
    if (strcmp(action, "调小音量") == 0) {
        int vol = audio_service_get_volume() - 10;
        if (vol < 0) vol = 0;
        audio_service_set_volume(vol);
        save_volume_to_nvs(vol);
        if (s_task_sound_ready) {
            speech_player_stop();
            task_sound_play(TASK_SOUND_ACTION_CONFIRM);
        }
        return;
    }

    /* 其他命令只在 marinating 状态生效 */
    const char *mapped = map_voice_action(action);
    if (s_current_state != WB_MQTT_STATE_MARINATING) {
        ESP_LOGI(TAG, "Voice action ignored: not in marinating state");
        return;
    }
    esp_err_t err = whitebox_mqtt_publish_action(mapped, "voice");
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Failed to publish voice action");
    } else if (s_task_sound_ready) {
        speech_player_stop();
        task_sound_play(TASK_SOUND_ACTION_CONFIRM);
    }
}

/* ── 按钮回调 ── */

static void boot_single_click_cb(void *button_handle, void *usr_data)
{
    (void)button_handle;
    (void)usr_data;
    /* 只有 marinating 下单击才有意义 */
    if (s_current_state == WB_MQTT_STATE_MARINATING) {
        ESP_LOGI(TAG, "BOOT click -> continue");
        if (whitebox_mqtt_publish_action("continue", "button") == ESP_OK && s_task_sound_ready) {
            speech_player_stop();
            task_sound_play(TASK_SOUND_ACTION_CONFIRM);
        }
    } else {
        ESP_LOGD(TAG, "BOOT click ignored (state=%d)", s_current_state);
    }
}

static void boot_long_press_cb(void *button_handle, void *usr_data)
{
    (void)button_handle;
    (void)usr_data;

    if (s_current_state == WB_MQTT_STATE_MARINATING ||
        s_current_state == WB_MQTT_STATE_COOKING ||
        s_current_state == WB_MQTT_STATE_THINKING) {
        ESP_LOGI(TAG, "BOOT long press -> reject");
        if (whitebox_mqtt_publish_action("reject", "button") == ESP_OK && s_task_sound_ready) {
            speech_player_stop();
            task_sound_play(TASK_SOUND_ACTION_CONFIRM);
        }
    } else {
        /* idle / offline / unknown → AP 配网 */
        ESP_LOGI(TAG, "BOOT long press -> AP config mode");
        stop_idle_gif_timer();
        display_switch_gif_by_char('h');
        whitebox_mqtt_stop();
        wifi_wrapper_start_ap();
    }
}

/* ── WiFi 事件回调 ── */
static void on_wifi_event(wifi_wrapper_event_t event)
{
    switch (event) {
        case WIFI_EVT_CONNECTED:
            ESP_LOGI(TAG, "WiFi connected: %s ip=%s",
                     wifi_wrapper_get_ssid(), wifi_wrapper_get_ip());
            s_wifi_connected = true;
            if (whitebox_mqtt_start() != ESP_OK) {
                ESP_LOGW(TAG, "MQTT start skipped (host not configured?)");
            }
            break;

        case WIFI_EVT_DISCONNECTED:
            ESP_LOGW(TAG, "WiFi disconnected");
            s_wifi_connected = false;
            whitebox_mqtt_stop();
            break;

        case WIFI_EVT_CONFIG_MODE_ENTER:
            ESP_LOGI(TAG, "=== AP Config Mode ===");
            s_ap_mode_active = true;
            break;

        case WIFI_EVT_CONFIG_MODE_EXIT:
            ESP_LOGI(TAG, "=== AP Config Mode Exit ===");
            s_ap_mode_active = false;
            wifi_wrapper_start_station();
            break;

        default:
            break;
    }
}

void app_main(void)
{
    esp_err_t ret;
    bool wifi_ready = false;

    ESP_LOGI(TAG, "Starting desk_toy (whitebox)");

    /* ── NVS ── */
    ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }

    /* ── WiFi 初始化 ── */
    ret = wifi_wrapper_init("WhiteBox", "zh-CN");
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "wifi_wrapper_init failed: %s", esp_err_to_name(ret));
        ESP_LOGW(TAG, "Continuing without WiFi/MQTT");
    } else {
        wifi_ready = true;
        wifi_wrapper_set_event_cb(on_wifi_event);
        wifi_wrapper_ensure_default_ssid();
        wifi_wrapper_start_station();
    }

    /* ── 显示屏 + GIF ── */
    ret = display_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "display_init failed: %s", esp_err_to_name(ret));
        return;
    }

    /* 注册 MQTT 状态回调 */
    whitebox_mqtt_set_state_cb(on_mqtt_state);

    /* 注册独立 TTS 语音回调 */
    whitebox_mqtt_set_speech_cb(on_mqtt_speech);

    /* 注册 MQTT 音量回调 */
    whitebox_mqtt_set_volume_cb(on_mqtt_volume);

    /* 注册按钮回调 */
    if (wifi_ready) {
        button_handle_t boot_btn = display_get_boot_button();
        if (boot_btn) {
            iot_button_register_cb(boot_btn, BUTTON_SINGLE_CLICK, NULL, boot_single_click_cb, NULL);
            iot_button_register_cb(boot_btn, BUTTON_LONG_PRESS_START, NULL, boot_long_press_cb, NULL);
        }
    }

    /* ── 语音服务 ── */
    /* 从 NVS 加载音量 */
    {
        whitebox_config_t cfg;
        if (whitebox_config_load(&cfg) == ESP_OK) {
            audio_service_set_volume_init(cfg.volume);
            ESP_LOGI(TAG, "Volume from NVS: %d", cfg.volume);
        }
    }
    ret = audio_service_init(on_voice_action);
    if (ret == ESP_OK) {
        audio_service_start();
        if (task_sound_init() == ESP_OK) {
            s_task_sound_ready = true;
        } else {
            ESP_LOGW(TAG, "Task sound init failed, sounds disabled");
        }
        if (speech_player_init() == ESP_OK) {
            s_speech_player_ready = true;
        } else {
            ESP_LOGW(TAG, "Speech player init failed, tts disabled");
        }
    } else {
        ESP_LOGW(TAG, "Audio service init failed (0x%x), skipping", ret);
    }

    ESP_LOGI(TAG, "=== All systems started ===");

    /* ── 主循环 ── */
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
