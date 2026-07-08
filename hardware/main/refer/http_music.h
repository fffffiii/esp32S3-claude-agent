#ifndef HTTP_MUSIC_H
#define HTTP_MUSIC_H

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 初始化HTTP音乐播放模块
 * @return ESP_OK 成功，其他值表示失败
 */
esp_err_t http_music_init(void);


/**
 * @brief 释放HTTP音乐播放模块占用的资源
 * @return ESP_OK 成功，其他值表示失败
 */
esp_err_t http_music_deinit(void);

/**
 * @brief 停止音乐播放
 * @return ESP_OK 成功，其他值表示失败
 */
esp_err_t http_music_stop(void);

/**
 * @brief 获取歌曲信息并打印歌词
 * @param song_name 歌曲名称
 * @return ESP_OK 成功，其他值表示失败
 */
esp_err_t fetch_music_info_and_lyrics(const char* song_name);

/**
 * @brief 启动音乐播放 - 集成音频管道检查、音乐地址获取和播放功能
 * @param music_name 音乐名称
 * @return ESP_OK 成功，其他值表示失败
 */
esp_err_t start_play_music(const char* music_name);

/**
 * @brief 暂停音乐播放并记录当前位置
 * @return ESP_OK 成功，其他值表示失败
 */
esp_err_t http_music_pause(void);

/**
 * @brief 恢复音乐播放并设置到之前保存的位置
 * @return ESP_OK 成功，其他值表示失败
 */
esp_err_t http_music_resume(void);

/**
 * @brief 获取当前播放位置（字节）
 * @param position 输出参数，当前播放位置
 * @return ESP_OK 成功，其他值表示失败
 */
esp_err_t http_music_get_position(int64_t* position);

/**
 * @brief 设置播放位置（字节）
 * @param position 要设置的播放位置
 * @return ESP_OK 成功，其他值表示失败
 */
esp_err_t http_music_set_position(int64_t position);

/**
 * @brief 获取音频总时长（如果可用）
 * @param duration 输出参数，音频总时长
 * @return ESP_OK 成功，其他值表示失败
 */
esp_err_t http_music_get_duration(int* duration);

esp_err_t alarm_start_play_music();

void set_music_display_status(bool is_end);
#ifdef __cplusplus
}
#endif

#endif // HTTP_MUSIC_H