#ifndef WHITEBOX_CONFIG_H
#define WHITEBOX_CONFIG_H

#include "esp_err.h"
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

/* NVS namespace for MQTT configuration */
#define WB_CFG_NVS_NAMESPACE "whitebox"

/* Default values �� prefer Kconfig overrides when available */
#ifdef CONFIG_MQTT_HOST
#define WB_CFG_DEFAULT_MQTT_HOST       CONFIG_MQTT_HOST
#else
#define WB_CFG_DEFAULT_MQTT_HOST       ""
#endif

#ifdef CONFIG_MQTT_PORT
#define WB_CFG_DEFAULT_MQTT_PORT       CONFIG_MQTT_PORT
#else
#define WB_CFG_DEFAULT_MQTT_PORT       1883
#endif

#ifdef CONFIG_MQTT_USERNAME
#define WB_CFG_DEFAULT_MQTT_USERNAME   CONFIG_MQTT_USERNAME
#else
#define WB_CFG_DEFAULT_MQTT_USERNAME   ""
#endif

#ifdef CONFIG_MQTT_PASSWORD
#define WB_CFG_DEFAULT_MQTT_PASSWORD   CONFIG_MQTT_PASSWORD
#else
#define WB_CFG_DEFAULT_MQTT_PASSWORD   ""
#endif
#define WB_CFG_DEFAULT_TOPIC_PREFIX    "whitebox"
#define WB_CFG_DEFAULT_DEVICE_ID       "whitebox-001"
#define WB_CFG_DEFAULT_VOLUME          70

/* Max string lengths (including null terminator) */
#define WB_CFG_MQTT_HOST_MAX_LEN      128
#define WB_CFG_MQTT_USERNAME_MAX_LEN   64
#define WB_CFG_MQTT_PASSWORD_MAX_LEN   64
#define WB_CFG_TOPIC_PREFIX_MAX_LEN    32
#define WB_CFG_DEVICE_ID_MAX_LEN       32

typedef struct {
    char mqtt_host[WB_CFG_MQTT_HOST_MAX_LEN];
    uint16_t mqtt_port;
    char mqtt_username[WB_CFG_MQTT_USERNAME_MAX_LEN];
    char mqtt_password[WB_CFG_MQTT_PASSWORD_MAX_LEN];
    char topic_prefix[WB_CFG_TOPIC_PREFIX_MAX_LEN];
    char device_id[WB_CFG_DEVICE_ID_MAX_LEN];
    int volume; /* 0-100 */
} whitebox_config_t;

/**
 * @brief Load configuration from NVS. Returns defaults for missing keys.
 */
esp_err_t whitebox_config_load(whitebox_config_t *cfg);

/**
 * @brief Save configuration to NVS.
 */
esp_err_t whitebox_config_save(const whitebox_config_t *cfg);

/**
 * @brief Check if MQTT host is configured (non-empty).
 */
bool whitebox_config_mqtt_is_ready(const whitebox_config_t *cfg);

/**
 * @brief Get full topic string, e.g. "whitebox/pc/state".
 *        Caller must provide buffer of sufficient size.
 */
void whitebox_config_make_topic(const whitebox_config_t *cfg,
                                const char *suffix,
                                char *out, size_t out_len);

#endif /* WHITEBOX_CONFIG_H */
