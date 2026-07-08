#ifndef WHITEBOX_MQTT_H
#define WHITEBOX_MQTT_H

#include "esp_err.h"
#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ── 设备状态枚举 ── */
typedef enum {
    WB_MQTT_STATE_UNKNOWN = 0,
    WB_MQTT_STATE_IDLE,
    WB_MQTT_STATE_COOKING,
    WB_MQTT_STATE_THINKING,
    WB_MQTT_STATE_MARINATING,
    WB_MQTT_STATE_OFFLINE,
} whitebox_mqtt_state_t;

/* ── 状态事件结构 ── */
typedef struct {
    whitebox_mqtt_state_t state;
    char status[24];
    uint32_t seq;
    char hook_event_name[32];
    char hook_message[64];
    char latest_message_type[24];
    char speech_id[40];
    char speech_kind[32];
    char speech_text[256];
    char speech_audio_url[384];
    char speech_format[16];
    uint32_t speech_sample_rate;
    char speech_created_at[32];
} whitebox_mqtt_state_event_t;

/* ── 状态变更回调 ── */
typedef void (*whitebox_mqtt_state_cb_t)(const whitebox_mqtt_state_event_t *event);

/* ── 独立语音事件 ── */
typedef struct {
    uint32_t seq;
    char status[24];
    char session_id[64];
    char project_key[64];
    char speech_id[40];
    char speech_kind[32];
    char speech_text[256];
    char speech_audio_url[384];
    char speech_format[16];
    uint32_t speech_sample_rate;
    char speech_created_at[32];
} whitebox_mqtt_speech_event_t;

typedef void (*whitebox_mqtt_speech_cb_t)(const whitebox_mqtt_speech_event_t *event);

/* ── 音量事件 ── */
typedef struct {
    int volume; /* 0-100 */
} whitebox_mqtt_volume_event_t;

typedef void (*whitebox_mqtt_volume_cb_t)(const whitebox_mqtt_volume_event_t *event);

/**
 * @brief Initialize and start MQTT client.
 *        Reads MQTT config from whitebox_config (NVS).
 *        Must be called AFTER WiFi is connected.
 */
esp_err_t whitebox_mqtt_start(void);

/**
 * @brief Stop and destroy MQTT client.
 */
void whitebox_mqtt_stop(void);

/**
 * @brief Check if MQTT client is connected to broker.
 */
bool whitebox_mqtt_is_connected(void);

/**
 * @brief Get the last received seq number from pc/state.
 */
uint32_t whitebox_mqtt_get_last_seq(void);

/**
 * @brief Register a callback for MQTT state changes.
 *        Called when pc/state payload is received and parsed.
 */
void whitebox_mqtt_set_state_cb(whitebox_mqtt_state_cb_t cb);

/**
 * @brief Register a callback for independent TTS speech messages.
 *        Called when pc/speech payload is received and parsed.
 */
void whitebox_mqtt_set_speech_cb(whitebox_mqtt_speech_cb_t cb);

/**
 * @brief Get the current device state (last received from pc/state).
 */
whitebox_mqtt_state_t whitebox_mqtt_get_state(void);

/**
 * @brief Publish an action to {prefix}/device/{device_id}/action.
 * @param action "continue" or "reject"
 * @param source "button" or "voice"
 * @return ESP_OK on success, ESP_ERR_INVALID_STATE if MQTT not connected
 */
esp_err_t whitebox_mqtt_publish_action(const char *action, const char *source);

/**
 * @brief Register a callback for volume changes received via MQTT.
 */
void whitebox_mqtt_set_volume_cb(whitebox_mqtt_volume_cb_t cb);

#ifdef __cplusplus
}
#endif

#endif /* WHITEBOX_MQTT_H */
