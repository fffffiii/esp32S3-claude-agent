#ifndef AUDIO_SERVICE_H
#define AUDIO_SERVICE_H

#include "esp_err.h"
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 唤醒事件回调类型
 * @param command 识别到的命令词文本
 */
typedef void (*audio_service_wake_cb_t)(const char *command);

/**
 * @brief 初始化音频服务（I2C + ES8388 + 唤醒词引擎）
 * @param wake_cb 唤醒回调，识别到命令词时调用
 */
esp_err_t audio_service_init(audio_service_wake_cb_t wake_cb);

/**
 * @brief 设置初始音量（在 init 之前调用，仅设值不写硬件）
 */
void audio_service_set_volume_init(int volume);

/**
 * @brief 启动音频录制和唤醒词检测任务
 */
esp_err_t audio_service_start(void);

/**
 * @brief 停止音频录制和唤醒词检测
 */
esp_err_t audio_service_stop(void);

/**
 * @brief 反初始化音频服务
 */
esp_err_t audio_service_deinit(void);

/**
 * @brief 输出 PCM 数据到扬声器
 */
esp_err_t audio_service_output_pcm(const int16_t *pcm, int samples);

/**
 * @brief 占用扬声器播放锁，避免短提示音和 TTS 同时出声。
 */
esp_err_t audio_service_playback_acquire(uint32_t timeout_ms);

/**
 * @brief 释放扬声器播放锁。
 */
void audio_service_playback_release(void);

/**
 * @brief 设置输出音量（0-100），立即生效。
 */
void audio_service_set_volume(int volume);

/**
 * @brief 获取当前输出音量（0-100）。
 */
int audio_service_get_volume(void);

#ifdef __cplusplus
}
#endif

#endif /* AUDIO_SERVICE_H */
