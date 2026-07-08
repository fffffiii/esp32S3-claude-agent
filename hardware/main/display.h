#ifndef DISPLAY_H
#define DISPLAY_H

#include "esp_err.h"
#include "iot_button.h"

/**
 * @brief 初始化显示屏
 * @return esp_err_t ESP_OK 表示成功，其他值表示失败
 */
esp_err_t display_init(void);

/**
 * @brief 根据字符切换GIF图像
 * @param ch 字符a-l，对应不同的GIF图像
 * @return esp_err_t ESP_OK 表示成功，其他值表示失败
 */
esp_err_t display_switch_gif_by_char(char ch);

/**
 * @brief 获取 BOOT 按钮句柄（用于注册额外回调如长按配网）
 * @return button_handle_t 按钮句柄，未初始化时返回 NULL
 */
button_handle_t display_get_boot_button(void);

#endif // DISPLAY_H
