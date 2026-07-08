#ifndef BLE_CONNECTION_H
#define BLE_CONNECTION_H

#include "esp_err.h"

/**
 * @brief 初始化 BLE 连接
 * @return ESP_OK 成功，其他值失败
 */
esp_err_t ble_connection_init(void);

#endif // BLE_CONNECTION_H
