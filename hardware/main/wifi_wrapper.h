#ifndef WIFI_WRAPPER_H
#define WIFI_WRAPPER_H

#include "esp_err.h"
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    WIFI_EVT_SCANNING,
    WIFI_EVT_CONNECTING,
    WIFI_EVT_CONNECTED,
    WIFI_EVT_DISCONNECTED,
    WIFI_EVT_CONFIG_MODE_ENTER,
    WIFI_EVT_CONFIG_MODE_EXIT,
} wifi_wrapper_event_t;

typedef void (*wifi_wrapper_event_cb_t)(wifi_wrapper_event_t event);

/**
 * @brief Initialize WiFi subsystem (NVS, netif, event loop, WiFi driver).
 *        Must be called after nvs_flash_init().
 * @param ssid_prefix  AP SSID prefix, e.g. "WhiteBox"
 * @param language     Web UI language, e.g. "zh-CN"
 */
esp_err_t wifi_wrapper_init(const char *ssid_prefix, const char *language);

/**
 * @brief Ensure NVS has at least one WiFi credential.
 *        If NVS is empty, writes the Kconfig default (CONFIG_WIFI_SSID/PASSWORD).
 */
void wifi_wrapper_ensure_default_ssid(void);

/**
 * @brief Start station mode — auto-connect to saved WiFi.
 */
void wifi_wrapper_start_station(void);

/**
 * @brief Start AP config mode — captive portal for provisioning.
 */
void wifi_wrapper_start_ap(void);

/**
 * @brief Stop AP config mode.
 */
void wifi_wrapper_stop_ap(void);

/**
 * @brief Check if station is connected to an AP.
 */
bool wifi_wrapper_is_connected(void);

/**
 * @brief Get connected AP's SSID (empty string if not connected).
 */
const char *wifi_wrapper_get_ssid(void);

/**
 * @brief Get current IP address string (empty string if not connected).
 */
const char *wifi_wrapper_get_ip(void);

/**
 * @brief Get device MAC address string.
 */
const char *wifi_wrapper_get_mac(void);

/**
 * @brief Register event callback.
 */
void wifi_wrapper_set_event_cb(wifi_wrapper_event_cb_t cb);

#ifdef __cplusplus
}
#endif

#endif /* WIFI_WRAPPER_H */
