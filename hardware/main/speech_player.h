#ifndef SPEECH_PLAYER_H
#define SPEECH_PLAYER_H

#include "esp_err.h"
#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    char speech_id[40];
    char audio_url[384];
    char text[256];
    uint32_t sample_rate;
} speech_player_request_t;

/**
 * @brief 初始化 TTS 播放模块。
 */
esp_err_t speech_player_init(void);

/**
 * @brief 播放一段 TTS 语音。若当前正在播放，则新请求会覆盖旧请求。
 */
esp_err_t speech_player_play(const speech_player_request_t *request);

/**
 * @brief 停止当前 TTS 播放。
 */
void speech_player_stop(void);

#ifdef __cplusplus
}
#endif

#endif /* SPEECH_PLAYER_H */
