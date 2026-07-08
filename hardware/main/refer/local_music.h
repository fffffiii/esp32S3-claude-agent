#pragma once
#include "esp_err.h"

// 初始化本地音乐播放模块
esp_err_t local_music_init(void);

// 开始播放
esp_err_t local_music_start(void);

// 根据名称开始播放特定音乐
esp_err_t local_music_start_play(const char* name);

// 处理音频事件
void local_music_process_events(void);

// 停止播放
esp_err_t local_music_stop(void);

// 反初始化本地音乐播放模块
esp_err_t local_music_deinit(void);

void local_music_wait_for_finish();