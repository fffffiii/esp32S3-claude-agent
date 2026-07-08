#ifndef _AUDREC_H_
#define _AUDREC_H_

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 命令词识别结果回调函数类型
 * 
 * @param cmd 识别到的命令词
 */
typedef void (*cmd_callback_t)(const char *cmd);

/**
 * @brief 初始化音频录制和唤醒词检测
 * 
 * @param cmd_cb 命令词识别结果回调函数
 * @return esp_err_t 
 */
esp_err_t audrec_init(cmd_callback_t cmd_cb);

/**
 * @brief 启动音频录制和唤醒词检测任务
 * 
 * @return esp_err_t 
 */
esp_err_t audrec_start(void);

/**
 * @brief 停止音频录制和唤醒词检测
 * 
 * @return esp_err_t 
 */
esp_err_t audrec_stop(void);

/**
 * @brief 添加唤醒词命令
 * 
 * @param command 唤醒词命令
 * @param command_name 唤醒词显示名称
 * @return esp_err_t 
 */
esp_err_t audrec_add_command(const char *command, const char *command_name);

/**
 * @brief 将PCM数据输出到I2S
 * 
 * @param pcm PCM数据
 * @param samples 样本数量
 * @return esp_err_t 
 */
esp_err_t audrec_output_pcm(const int16_t* pcm, int samples);

/**
 * @brief 反初始化音频录制和唤醒词检测
 * 
 * @return esp_err_t 
 */
esp_err_t audrec_deinit(void);

#ifdef __cplusplus
}
#endif

#endif /* _AUDREC_H_ */
