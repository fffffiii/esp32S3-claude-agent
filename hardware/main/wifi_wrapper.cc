#include "wifi_wrapper.h"
#include "wifi_manager.h"
#include "ssid_manager.h"
#include "esp_log.h"
#include <string.h>

static const char *TAG = "wifi_wrap";

static wifi_wrapper_event_cb_t s_event_cb = NULL;

extern "C" esp_err_t wifi_wrapper_init(const char *ssid_prefix, const char *language)
{
    ESP_LOGI(TAG, "Initializing WiFi wrapper: prefix=%s lang=%s", ssid_prefix, language);

    auto &mgr = WifiManager::GetInstance();

    WifiManagerConfig cfg;
    cfg.ssid_prefix = ssid_prefix ? ssid_prefix : "ESP32";
    cfg.language = language ? language : "zh-CN";
    cfg.station_scan_min_interval_seconds = 10;
    cfg.station_scan_max_interval_seconds = 300;

    if (!mgr.Initialize(cfg)) {
        ESP_LOGE(TAG, "WifiManager::Initialize failed");
        return ESP_FAIL;
    }

    mgr.SetEventCallback([](WifiEvent event, const std::string &data) {
        if (!s_event_cb) return;
        switch (event) {
            case WifiEvent::Scanning:       s_event_cb(WIFI_EVT_SCANNING); break;
            case WifiEvent::Connecting:     s_event_cb(WIFI_EVT_CONNECTING); break;
            case WifiEvent::Connected:      s_event_cb(WIFI_EVT_CONNECTED); break;
            case WifiEvent::Disconnected:   s_event_cb(WIFI_EVT_DISCONNECTED); break;
            case WifiEvent::ConfigModeEnter: s_event_cb(WIFI_EVT_CONFIG_MODE_ENTER); break;
            case WifiEvent::ConfigModeExit: s_event_cb(WIFI_EVT_CONFIG_MODE_EXIT); break;
        }
    });

    ESP_LOGI(TAG, "WiFi wrapper initialized");
    return ESP_OK;
}

extern "C" void wifi_wrapper_ensure_default_ssid(void)
{
#ifdef CONFIG_WIFI_SSID
    auto &ssid_mgr = SsidManager::GetInstance();
    if (ssid_mgr.GetSsidList().empty()) {
        const char *ssid = CONFIG_WIFI_SSID;
#ifdef CONFIG_WIFI_PASSWORD
        const char *password = CONFIG_WIFI_PASSWORD;
#else
        const char *password = "";
#endif
        if (ssid[0] != '\0') {
            ESP_LOGI(TAG, "NVS empty, writing default SSID: %s", ssid);
            ssid_mgr.AddSsid(ssid, password);
        }
    }
#endif
}

extern "C" void wifi_wrapper_start_station(void)
{
    ESP_LOGI(TAG, "Starting station mode");
    WifiManager::GetInstance().StartStation();
}

extern "C" void wifi_wrapper_start_ap(void)
{
    ESP_LOGI(TAG, "Starting AP config mode");
    WifiManager::GetInstance().StartConfigAp();
}

extern "C" void wifi_wrapper_stop_ap(void)
{
    ESP_LOGI(TAG, "Stopping AP config mode");
    WifiManager::GetInstance().StopConfigAp();
}

extern "C" bool wifi_wrapper_is_connected(void)
{
    return WifiManager::GetInstance().IsConnected();
}

extern "C" const char *wifi_wrapper_get_ssid(void)
{
    static std::string ssid;
    ssid = WifiManager::GetInstance().GetSsid();
    return ssid.c_str();
}

extern "C" const char *wifi_wrapper_get_ip(void)
{
    static std::string ip;
    ip = WifiManager::GetInstance().GetIpAddress();
    return ip.c_str();
}

extern "C" const char *wifi_wrapper_get_mac(void)
{
    static std::string mac;
    mac = WifiManager::GetInstance().GetMacAddress();
    return mac.c_str();
}

extern "C" void wifi_wrapper_set_event_cb(wifi_wrapper_event_cb_t cb)
{
    s_event_cb = cb;
}
