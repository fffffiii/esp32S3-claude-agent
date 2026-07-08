#include "whitebox_config.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_log.h"
#include <string.h>

static const char *TAG = "wb_cfg";

static void load_str(nvs_handle_t h, const char *key, char *dst, size_t dst_len, const char *dflt)
{
    size_t len = dst_len;
    esp_err_t err = nvs_get_str(h, key, dst, &len);
    if (err != ESP_OK) {
        strlcpy(dst, dflt, dst_len);
    }
}

esp_err_t whitebox_config_load(whitebox_config_t *cfg)
{
    memset(cfg, 0, sizeof(*cfg));

    /* Apply defaults */
    strlcpy(cfg->mqtt_host, WB_CFG_DEFAULT_MQTT_HOST, sizeof(cfg->mqtt_host));
    cfg->mqtt_port = WB_CFG_DEFAULT_MQTT_PORT;
    strlcpy(cfg->mqtt_username, WB_CFG_DEFAULT_MQTT_USERNAME, sizeof(cfg->mqtt_username));
    strlcpy(cfg->mqtt_password, WB_CFG_DEFAULT_MQTT_PASSWORD, sizeof(cfg->mqtt_password));
    strlcpy(cfg->topic_prefix, WB_CFG_DEFAULT_TOPIC_PREFIX, sizeof(cfg->topic_prefix));
    strlcpy(cfg->device_id, WB_CFG_DEFAULT_DEVICE_ID, sizeof(cfg->device_id));
    cfg->volume = WB_CFG_DEFAULT_VOLUME;

    nvs_handle_t nvs;
    esp_err_t err = nvs_open(WB_CFG_NVS_NAMESPACE, NVS_READONLY, &nvs);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "NVS namespace '%s' not found, using defaults", WB_CFG_NVS_NAMESPACE);
        return ESP_OK; /* Not an error — first boot */
    }

    load_str(nvs, "mqtt_host", cfg->mqtt_host, sizeof(cfg->mqtt_host), WB_CFG_DEFAULT_MQTT_HOST);

    uint16_t port = 0;
    if (nvs_get_u16(nvs, "mqtt_port", &port) == ESP_OK) {
        cfg->mqtt_port = port;
    }

    load_str(nvs, "mqtt_user", cfg->mqtt_username, sizeof(cfg->mqtt_username), WB_CFG_DEFAULT_MQTT_USERNAME);
    load_str(nvs, "mqtt_pass", cfg->mqtt_password, sizeof(cfg->mqtt_password), WB_CFG_DEFAULT_MQTT_PASSWORD);
    load_str(nvs, "topic_pfx", cfg->topic_prefix, sizeof(cfg->topic_prefix), WB_CFG_DEFAULT_TOPIC_PREFIX);
    load_str(nvs, "device_id", cfg->device_id, sizeof(cfg->device_id), WB_CFG_DEFAULT_DEVICE_ID);

    int32_t vol = 0;
    if (nvs_get_i32(nvs, "volume", &vol) == ESP_OK) {
        cfg->volume = (int)vol;
    }

    nvs_close(nvs);
    ESP_LOGI(TAG, "Config loaded: host=%s port=%d dev=%s",
             cfg->mqtt_host, cfg->mqtt_port, cfg->device_id);
    return ESP_OK;
}

esp_err_t whitebox_config_save(const whitebox_config_t *cfg)
{
    nvs_handle_t nvs;
    esp_err_t err = nvs_open(WB_CFG_NVS_NAMESPACE, NVS_READWRITE, &nvs);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to open NVS: %s", esp_err_to_name(err));
        return err;
    }

    nvs_set_str(nvs, "mqtt_host", cfg->mqtt_host);
    nvs_set_u16(nvs, "mqtt_port", cfg->mqtt_port);
    nvs_set_str(nvs, "mqtt_user", cfg->mqtt_username);
    nvs_set_str(nvs, "mqtt_pass", cfg->mqtt_password);
    nvs_set_str(nvs, "topic_pfx", cfg->topic_prefix);
    nvs_set_str(nvs, "device_id", cfg->device_id);
    nvs_set_i32(nvs, "volume", (int32_t)cfg->volume);

    err = nvs_commit(nvs);
    nvs_close(nvs);

    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Config saved");
    } else {
        ESP_LOGE(TAG, "NVS commit failed: %s", esp_err_to_name(err));
    }
    return err;
}

bool whitebox_config_mqtt_is_ready(const whitebox_config_t *cfg)
{
    return cfg->mqtt_host[0] != '\0';
}

void whitebox_config_make_topic(const whitebox_config_t *cfg,
                                const char *suffix,
                                char *out, size_t out_len)
{
    snprintf(out, out_len, "%s/%s", cfg->topic_prefix, suffix);
}
