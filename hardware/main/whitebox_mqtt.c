#include "whitebox_mqtt.h"
#include "whitebox_config.h"
#include "mqtt_client.h"
#include "esp_log.h"
#include "cJSON.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

static const char *TAG = "wb_mqtt";

#define WB_MQTT_MAX_STATE_PAYLOAD_BYTES (8 * 1024)

/* ── State ── */
static esp_mqtt_client_handle_t s_client = NULL;
static bool s_connected = false;
static uint32_t s_last_seq = 0;
static whitebox_config_t s_cfg;
static whitebox_mqtt_state_t s_current_state = WB_MQTT_STATE_UNKNOWN;
static whitebox_mqtt_state_cb_t s_state_cb = NULL;
static whitebox_mqtt_speech_cb_t s_speech_cb = NULL;
static whitebox_mqtt_volume_cb_t s_volume_cb = NULL;
static char *s_state_frag_buf = NULL;
static int s_state_frag_total = 0;
static int s_state_frag_received = 0;
static char *s_speech_frag_buf = NULL;
static int s_speech_frag_total = 0;
static int s_speech_frag_received = 0;

/* ── Topic buffers ── */
static char s_topic_state[64];       /* {prefix}/pc/state */
static char s_topic_speech[64];      /* {prefix}/pc/speech */
static char s_topic_ack[64];         /* {prefix}/device/{id}/ack */
static char s_topic_avail[64];       /* {prefix}/device/{id}/availability */
static char s_topic_action[64];      /* {prefix}/device/{id}/action */

static void clear_state_fragment(void)
{
    free(s_state_frag_buf);
    s_state_frag_buf = NULL;
    s_state_frag_total = 0;
    s_state_frag_received = 0;
}

static void clear_speech_fragment(void)
{
    free(s_speech_frag_buf);
    s_speech_frag_buf = NULL;
    s_speech_frag_total = 0;
    s_speech_frag_received = 0;
}

/* ── Status string → state enum ── */
static whitebox_mqtt_state_t status_to_state(const char *status)
{
    if (!status || !status[0]) return WB_MQTT_STATE_UNKNOWN;
    if (strcmp(status, "idle") == 0)     return WB_MQTT_STATE_IDLE;
    if (strcmp(status, "cooking") == 0)  return WB_MQTT_STATE_COOKING;
    if (strcmp(status, "thinking") == 0) return WB_MQTT_STATE_THINKING;
    if (strcmp(status, "marinating") == 0) return WB_MQTT_STATE_MARINATING;
    if (strcmp(status, "offline") == 0)  return WB_MQTT_STATE_OFFLINE;
    return WB_MQTT_STATE_UNKNOWN;
}

/* ── Publish helper ── */
static void publish(const char *topic, const char *payload, int qos, int retain)
{
    if (!s_client || !s_connected) return;
    int msg_id = esp_mqtt_client_publish(s_client, topic, payload, 0, qos, retain);
    ESP_LOGD(TAG, "PUB %s → msg_id=%d", topic, msg_id);
}

/* ── Publish ack ── */
static void publish_ack(void)
{
    char payload[128];
    snprintf(payload, sizeof(payload),
             "{\"last_seq\":%lu,\"ts\":%lu}",
             (unsigned long)s_last_seq,
             (unsigned long)(esp_log_timestamp() / 1000));
    publish(s_topic_ack, payload, 1, 0);
}

/* ── Publish availability ── */
static void publish_availability(const char *status)
{
    publish(s_topic_avail, status, 1, 1); /* retained */
}

static void copy_json_string(cJSON *obj, const char *key, char *dst, size_t dst_len)
{
    cJSON *item = obj ? cJSON_GetObjectItem(obj, key) : NULL;
    if (item && cJSON_IsString(item) && item->valuestring) {
        strlcpy(dst, item->valuestring, dst_len);
    }
}

static bool is_state_topic(const esp_mqtt_event_handle_t event)
{
    return event->topic &&
           event->topic_len == strlen(s_topic_state) &&
           memcmp(event->topic, s_topic_state, event->topic_len) == 0;
}

static bool is_speech_topic(const esp_mqtt_event_handle_t event)
{
    return event->topic &&
           event->topic_len == strlen(s_topic_speech) &&
           memcmp(event->topic, s_topic_speech, event->topic_len) == 0;
}

/* ── Handle incoming pc/state payload ── */
static void handle_pc_state(const char *data, int data_len)
{
    /* Make a null-terminated copy */
    char *buf = (char *)malloc(data_len + 1);
    if (!buf) {
        ESP_LOGE(TAG, "OOM parsing state");
        return;
    }
    memcpy(buf, data, data_len);
    buf[data_len] = '\0';

    cJSON *json = cJSON_Parse(buf);
    free(buf);
    if (!json) {
        ESP_LOGW(TAG, "Invalid JSON in pc/state");
        return;
    }

    /* Extract fields */
    cJSON *j_status    = cJSON_GetObjectItem(json, "status");
    cJSON *j_seq       = cJSON_GetObjectItem(json, "seq");
    cJSON *j_hook_name = cJSON_GetObjectItem(json, "hook_event_name");
    cJSON *j_hook_msg  = cJSON_GetObjectItem(json, "hook_message");
    cJSON *j_msg_type  = cJSON_GetObjectItem(json, "latest_message_type");
    cJSON *j_speech    = cJSON_GetObjectItem(json, "speech");

    const char *status    = cJSON_IsString(j_status)    ? j_status->valuestring    : "unknown";
    uint32_t seq          = cJSON_IsNumber(j_seq)       ? (uint32_t)j_seq->valueint : 0;
    const char *hook_name = cJSON_IsString(j_hook_name) ? j_hook_name->valuestring : "";
    const char *hook_msg  = cJSON_IsString(j_hook_msg)  ? j_hook_msg->valuestring  : "";
    const char *msg_type  = cJSON_IsString(j_msg_type)  ? j_msg_type->valuestring  : "";

    /* Log state */
    ESP_LOGI(TAG, "PC state: status=%s seq=%lu hook=%s", status, (unsigned long)seq, hook_name);

    /* Update seq */
    s_last_seq = seq;

    /* Convert to state enum and notify */
    whitebox_mqtt_state_t new_state = status_to_state(status);
    s_current_state = new_state;

    if (s_state_cb) {
        whitebox_mqtt_state_event_t evt = {0};
        evt.state = new_state;
        evt.seq = seq;
        strlcpy(evt.status, status, sizeof(evt.status));
        strlcpy(evt.hook_event_name, hook_name, sizeof(evt.hook_event_name));
        strlcpy(evt.hook_message, hook_msg, sizeof(evt.hook_message));
        strlcpy(evt.latest_message_type, msg_type, sizeof(evt.latest_message_type));
        if (j_speech && cJSON_IsObject(j_speech)) {
            copy_json_string(j_speech, "id", evt.speech_id, sizeof(evt.speech_id));
            copy_json_string(j_speech, "kind", evt.speech_kind, sizeof(evt.speech_kind));
            copy_json_string(j_speech, "text", evt.speech_text, sizeof(evt.speech_text));
            copy_json_string(j_speech, "audio_url", evt.speech_audio_url, sizeof(evt.speech_audio_url));
            copy_json_string(j_speech, "format", evt.speech_format, sizeof(evt.speech_format));
            copy_json_string(j_speech, "created_at", evt.speech_created_at, sizeof(evt.speech_created_at));
            cJSON *j_sample_rate = cJSON_GetObjectItem(j_speech, "sample_rate");
            if (cJSON_IsNumber(j_sample_rate)) {
                evt.speech_sample_rate = (uint32_t)j_sample_rate->valuedouble;
            }
            ESP_LOGI(TAG, "Speech payload: id=%s url_len=%u",
                     evt.speech_id, (unsigned int)strlen(evt.speech_audio_url));
        }
        s_state_cb(&evt);
    }

    /* Handle volume change */
    cJSON *j_volume = cJSON_GetObjectItem(json, "volume");
    if (cJSON_IsNumber(j_volume)) {
        int vol = j_volume->valueint;
        if (vol >= 0 && vol <= 100) {
            ESP_LOGI(TAG, "Volume command: %d", vol);
            if (s_volume_cb) {
                whitebox_mqtt_volume_event_t vev = { .volume = vol };
                s_volume_cb(&vev);
            }
        }
    }

    /* Publish ack */
    publish_ack();

    cJSON_Delete(json);
}

/* ── Handle incoming pc/speech payload ── */
static void handle_pc_speech(const char *data, int data_len)
{
    char *buf = (char *)malloc(data_len + 1);
    if (!buf) {
        ESP_LOGE(TAG, "OOM parsing speech");
        return;
    }
    memcpy(buf, data, data_len);
    buf[data_len] = '\0';

    cJSON *json = cJSON_Parse(buf);
    free(buf);
    if (!json) {
        ESP_LOGW(TAG, "Invalid JSON in pc/speech");
        return;
    }

    cJSON *j_speech = cJSON_GetObjectItem(json, "speech");
    if (!j_speech || !cJSON_IsObject(j_speech)) {
        j_speech = json;
    }

    if (s_speech_cb) {
        whitebox_mqtt_speech_event_t evt = {0};
        cJSON *j_seq = cJSON_GetObjectItem(json, "seq");
        if (!cJSON_IsNumber(j_seq)) {
            j_seq = cJSON_GetObjectItem(j_speech, "seq");
        }
        if (cJSON_IsNumber(j_seq)) {
            evt.seq = (uint32_t)j_seq->valuedouble;
        }

        copy_json_string(json, "status", evt.status, sizeof(evt.status));
        copy_json_string(json, "session_id", evt.session_id, sizeof(evt.session_id));
        copy_json_string(json, "project_key", evt.project_key, sizeof(evt.project_key));
        copy_json_string(j_speech, "id", evt.speech_id, sizeof(evt.speech_id));
        copy_json_string(j_speech, "kind", evt.speech_kind, sizeof(evt.speech_kind));
        copy_json_string(j_speech, "text", evt.speech_text, sizeof(evt.speech_text));
        copy_json_string(j_speech, "audio_url", evt.speech_audio_url, sizeof(evt.speech_audio_url));
        copy_json_string(j_speech, "format", evt.speech_format, sizeof(evt.speech_format));
        copy_json_string(j_speech, "created_at", evt.speech_created_at, sizeof(evt.speech_created_at));

        cJSON *j_sample_rate = cJSON_GetObjectItem(j_speech, "sample_rate");
        if (cJSON_IsNumber(j_sample_rate)) {
            evt.speech_sample_rate = (uint32_t)j_sample_rate->valuedouble;
        }

        ESP_LOGI(TAG, "Speech message: id=%s seq=%lu url_len=%u",
                 evt.speech_id, (unsigned long)evt.seq,
                 (unsigned int)strlen(evt.speech_audio_url));
        s_speech_cb(&evt);
    }

    cJSON_Delete(json);
}

static void handle_pc_state_event(const esp_mqtt_event_handle_t event)
{
    int total_len = event->total_data_len > 0 ? event->total_data_len : event->data_len;
    int offset = event->current_data_offset;

    if (total_len <= 0 || event->data_len <= 0 || !event->data) {
        return;
    }

    if (total_len > WB_MQTT_MAX_STATE_PAYLOAD_BYTES) {
        ESP_LOGW(TAG, "pc/state too large: total=%d max=%d",
                 total_len, WB_MQTT_MAX_STATE_PAYLOAD_BYTES);
        clear_state_fragment();
        return;
    }

    if (offset == 0) {
        if (!is_state_topic(event)) {
            return;
        }
        if (total_len == event->data_len) {
            handle_pc_state(event->data, event->data_len);
            return;
        }

        clear_state_fragment();
        s_state_frag_buf = (char *)malloc(total_len + 1);
        if (!s_state_frag_buf) {
            ESP_LOGE(TAG, "OOM assembling pc/state, total=%d", total_len);
            return;
        }
        s_state_frag_total = total_len;
        s_state_frag_received = 0;
        ESP_LOGI(TAG, "Assembling fragmented pc/state: total=%d first=%d",
                 total_len, event->data_len);
    }

    if (!s_state_frag_buf || s_state_frag_total != total_len) {
        ESP_LOGW(TAG, "Drop pc/state fragment without active buffer: offset=%d len=%d total=%d",
                 offset, event->data_len, total_len);
        clear_state_fragment();
        return;
    }

    if (offset < 0 || offset + event->data_len > s_state_frag_total) {
        ESP_LOGW(TAG, "Invalid pc/state fragment: offset=%d len=%d total=%d",
                 offset, event->data_len, s_state_frag_total);
        clear_state_fragment();
        return;
    }

    memcpy(s_state_frag_buf + offset, event->data, event->data_len);
    s_state_frag_received += event->data_len;

    if (s_state_frag_received >= s_state_frag_total) {
        s_state_frag_buf[s_state_frag_total] = '\0';
        handle_pc_state(s_state_frag_buf, s_state_frag_total);
        clear_state_fragment();
    }
}

static void handle_pc_speech_event(const esp_mqtt_event_handle_t event)
{
    int total_len = event->total_data_len > 0 ? event->total_data_len : event->data_len;
    int offset = event->current_data_offset;

    if (total_len <= 0 || event->data_len <= 0 || !event->data) {
        return;
    }

    if (total_len > WB_MQTT_MAX_STATE_PAYLOAD_BYTES) {
        ESP_LOGW(TAG, "pc/speech too large: total=%d max=%d",
                 total_len, WB_MQTT_MAX_STATE_PAYLOAD_BYTES);
        clear_speech_fragment();
        return;
    }

    if (offset == 0) {
        if (!is_speech_topic(event)) {
            return;
        }
        if (total_len == event->data_len) {
            handle_pc_speech(event->data, event->data_len);
            return;
        }

        clear_speech_fragment();
        s_speech_frag_buf = (char *)malloc(total_len + 1);
        if (!s_speech_frag_buf) {
            ESP_LOGE(TAG, "OOM assembling pc/speech, total=%d", total_len);
            return;
        }
        s_speech_frag_total = total_len;
        s_speech_frag_received = 0;
        ESP_LOGI(TAG, "Assembling fragmented pc/speech: total=%d first=%d",
                 total_len, event->data_len);
    }

    if (!s_speech_frag_buf || s_speech_frag_total != total_len) {
        ESP_LOGW(TAG, "Drop pc/speech fragment without active buffer: offset=%d len=%d total=%d",
                 offset, event->data_len, total_len);
        clear_speech_fragment();
        return;
    }

    if (offset < 0 || offset + event->data_len > s_speech_frag_total) {
        ESP_LOGW(TAG, "Invalid pc/speech fragment: offset=%d len=%d total=%d",
                 offset, event->data_len, s_speech_frag_total);
        clear_speech_fragment();
        return;
    }

    memcpy(s_speech_frag_buf + offset, event->data, event->data_len);
    s_speech_frag_received += event->data_len;

    if (s_speech_frag_received >= s_speech_frag_total) {
        s_speech_frag_buf[s_speech_frag_total] = '\0';
        handle_pc_speech(s_speech_frag_buf, s_speech_frag_total);
        clear_speech_fragment();
    }
}

/* ── MQTT event handler ── */
static void mqtt_event_handler(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data)
{
    esp_mqtt_event_handle_t event = (esp_mqtt_event_handle_t)event_data;

    switch (event->event_id) {
        case MQTT_EVENT_CONNECTED:
            ESP_LOGI(TAG, "MQTT connected to broker");
            s_connected = true;

            /* Publish online (retained) */
            publish_availability("online");

            /* Subscribe to pc/state and pc/speech */
            esp_mqtt_client_subscribe(s_client, s_topic_state, 1);
            ESP_LOGI(TAG, "Subscribed: %s", s_topic_state);
            esp_mqtt_client_subscribe(s_client, s_topic_speech, 1);
            ESP_LOGI(TAG, "Subscribed: %s", s_topic_speech);
            break;

        case MQTT_EVENT_DISCONNECTED:
            ESP_LOGW(TAG, "MQTT disconnected");
            s_connected = false;
            clear_state_fragment();
            clear_speech_fragment();
            break;

        case MQTT_EVENT_DATA:
            ESP_LOGD(TAG, "MQTT data: topic=%.*s", event->topic_len, event->topic);
            if (s_state_frag_buf || is_state_topic(event)) {
                handle_pc_state_event(event);
            } else if (s_speech_frag_buf || is_speech_topic(event)) {
                handle_pc_speech_event(event);
            }
            break;

        case MQTT_EVENT_ERROR:
            ESP_LOGE(TAG, "MQTT error: type=%d", event->error_handle->error_type);
            if (event->error_handle->error_type == MQTT_ERROR_TYPE_TCP_TRANSPORT) {
                ESP_LOGE(TAG, "  transport errno=%d", event->error_handle->esp_transport_sock_errno);
            }
            break;

        default:
            ESP_LOGD(TAG, "MQTT event: %d", event->event_id);
            break;
    }
}

/* ── Public API ── */

esp_err_t whitebox_mqtt_start(void)
{
    /* Load config */
    whitebox_config_load(&s_cfg);

    if (!whitebox_config_mqtt_is_ready(&s_cfg)) {
        ESP_LOGW(TAG, "MQTT host not configured, skipping MQTT start");
        return ESP_ERR_INVALID_STATE;
    }

    /* Build topics */
    char topic_suffix[64];

    snprintf(topic_suffix, sizeof(topic_suffix), "pc/state");
    whitebox_config_make_topic(&s_cfg, topic_suffix, s_topic_state, sizeof(s_topic_state));

    snprintf(topic_suffix, sizeof(topic_suffix), "pc/speech");
    whitebox_config_make_topic(&s_cfg, topic_suffix, s_topic_speech, sizeof(s_topic_speech));

    snprintf(topic_suffix, sizeof(topic_suffix), "device/%s/ack", s_cfg.device_id);
    whitebox_config_make_topic(&s_cfg, topic_suffix, s_topic_ack, sizeof(s_topic_ack));

    snprintf(topic_suffix, sizeof(topic_suffix), "device/%s/availability", s_cfg.device_id);
    whitebox_config_make_topic(&s_cfg, topic_suffix, s_topic_avail, sizeof(s_topic_avail));

    snprintf(topic_suffix, sizeof(topic_suffix), "device/%s/action", s_cfg.device_id);
    whitebox_config_make_topic(&s_cfg, topic_suffix, s_topic_action, sizeof(s_topic_action));

    ESP_LOGI(TAG, "Topics: state=%s speech=%s ack=%s avail=%s action=%s",
             s_topic_state, s_topic_speech, s_topic_ack, s_topic_avail, s_topic_action);

    /* Build broker URI */
    char uri[192];
    snprintf(uri, sizeof(uri), "mqtt://%s:%d", s_cfg.mqtt_host, s_cfg.mqtt_port);

    /* LWT payload */
    char lwt_topic[64];
    strlcpy(lwt_topic, s_topic_avail, sizeof(lwt_topic));

    esp_mqtt_client_config_t mqtt_cfg = {
        .broker = {
            .address.uri = uri,
        },
        .credentials = {
            .username = s_cfg.mqtt_username[0] ? s_cfg.mqtt_username : NULL,
            .authentication = {
                .password = s_cfg.mqtt_password[0] ? s_cfg.mqtt_password : NULL,
            },
        },
        .session = {
            .last_will = {
                .topic = lwt_topic,
                .msg = "offline",
                .qos = 1,
                .retain = true,
            },
        },
        .network = {
            .reconnect_timeout_ms = 5000,
        },
    };

    /* Use device_id as client_id */
    mqtt_cfg.credentials.client_id = s_cfg.device_id;

    s_client = esp_mqtt_client_init(&mqtt_cfg);
    if (!s_client) {
        ESP_LOGE(TAG, "esp_mqtt_client_init failed");
        return ESP_FAIL;
    }

    esp_mqtt_client_register_event(s_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);

    esp_err_t err = esp_mqtt_client_start(s_client);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_mqtt_client_start failed: %s", esp_err_to_name(err));
        esp_mqtt_client_destroy(s_client);
        s_client = NULL;
        return err;
    }

    ESP_LOGI(TAG, "MQTT client started: %s", uri);
    return ESP_OK;
}

void whitebox_mqtt_stop(void)
{
    if (s_client) {
        if (s_connected) {
            publish_availability("offline");
            vTaskDelay(pdMS_TO_TICKS(100)); /* Let the message go out */
        }
        esp_mqtt_client_stop(s_client);
        esp_mqtt_client_destroy(s_client);
        s_client = NULL;
        s_connected = false;
        ESP_LOGI(TAG, "MQTT client stopped");
    }
}

bool whitebox_mqtt_is_connected(void)
{
    return s_connected;
}

uint32_t whitebox_mqtt_get_last_seq(void)
{
    return s_last_seq;
}

void whitebox_mqtt_set_state_cb(whitebox_mqtt_state_cb_t cb)
{
    s_state_cb = cb;
}

void whitebox_mqtt_set_speech_cb(whitebox_mqtt_speech_cb_t cb)
{
    s_speech_cb = cb;
}

void whitebox_mqtt_set_volume_cb(whitebox_mqtt_volume_cb_t cb)
{
    s_volume_cb = cb;
}

whitebox_mqtt_state_t whitebox_mqtt_get_state(void)
{
    return s_current_state;
}

esp_err_t whitebox_mqtt_publish_action(const char *action, const char *source)
{
    if (!s_client || !s_connected) {
        ESP_LOGW(TAG, "Cannot publish action: MQTT not connected");
        return ESP_ERR_INVALID_STATE;
    }

    /* state string */
    const char *state_str;
    switch (s_current_state) {
        case WB_MQTT_STATE_IDLE:      state_str = "idle";      break;
        case WB_MQTT_STATE_COOKING:   state_str = "cooking";   break;
        case WB_MQTT_STATE_THINKING:  state_str = "thinking";  break;
        case WB_MQTT_STATE_MARINATING: state_str = "marinating"; break;
        case WB_MQTT_STATE_OFFLINE:   state_str = "offline";   break;
        default:                      state_str = "unknown";   break;
    }

    char payload[256];
    snprintf(payload, sizeof(payload),
             "{\"action\":\"%s\",\"source\":\"%s\",\"last_seq\":%lu,"
             "\"state\":\"%s\",\"device_id\":\"%s\",\"ts\":%lu}",
             action, source,
             (unsigned long)s_last_seq,
             state_str,
             s_cfg.device_id,
             (unsigned long)(esp_log_timestamp() / 1000));

    publish(s_topic_action, payload, 1, 0);
    ESP_LOGI(TAG, "Published action: %s source=%s", action, source);
    return ESP_OK;
}
