#ifndef __CHAINTIC_WAKE_WORD_H__
#define __CHAINTIC_WAKE_WORD_H__

#include <stdint.h>
#include <stddef.h>

#include <esp_mn_iface.h>
#include <esp_mn_models.h>
#include <esp_afe_sr_iface.h>
#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/task.h>
#include <esp_afe_sr_models.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief SDK 导出符号可见性
 */
#define CHAINTIC_SDK_API __attribute__((visibility("default")))

/**
 * @brief 唤醒引擎句柄
 */
typedef void* chaintic_wake_engine_t;

/**
 * @brief 用户数据句柄，由上层自定义使用
 */
typedef void* chaintic_wake_user_data_t;

/**
 * @brief 回调上下文结构体
 *
 * 在回调中携带当前引擎实例和用户数据指针。
 */
typedef struct chaintic_wake_callback_context {
  chaintic_wake_engine_t engine;
  chaintic_wake_user_data_t user_data;
} chaintic_wake_callback_context_t;

/**
 * @brief 引擎事件回调集合
 */
typedef struct chaintic_wake_event_handler {
  /**
   * @brief 唤醒/打断命令检测回调
   * @param ctx 回调上下文
   * @param text 识别到的命令文本（如拼音）
   * @param action 命令类型："wake" 或 "break"
   * @param command_id 命令编号
   * @param prob 识别置信度，范围 [0,1]
   */
  void (*on_wake_detected)(const chaintic_wake_callback_context_t* ctx,
                           const char* text,
                           const char* action,
                           int command_id,
                           float prob);
  /**
   * @brief 用于存储唤醒前的语音数据，后续可向服务端发送
   * @param ctx 回调上下文
   * @param data PCM 数据，16 kHz、int16_t、单声道
   * @param samples PCM 采样点数量
   */
  void (*on_pcm_chunk)(const chaintic_wake_callback_context_t* ctx,
                       const int16_t* data,
                       size_t samples);
} chaintic_wake_event_handler_t;

/**
 * @brief 唤醒引擎配置
 */
typedef struct chaintic_wake_config {
  /**
   * @brief 是否启用回声消除
   */
  bool aec_enable;
  /**
   * @brief 模型列表，包含前端与识别模型
   */
  srmodel_list_t* models_list;
  /**
   * @brief 语言类型，例如 "cn"、"en"
   */
  const char* language;
  /**
   * @brief 录音缓存时长，单位 ms
   */
  int duration_ms;
  /**
   * @brief 唤醒阈值，数值越大越严格
   */
  float threshold;
  /**
   * @brief 事件回调集合
   */
  chaintic_wake_event_handler_t event_handler;
  /**
   * @brief 回调中透传的用户数据指针
   */
  chaintic_wake_user_data_t user_data;
} chaintic_wake_config_t;

/**
 * @brief 创建唤醒引擎实例
 * @param cfg 引擎配置
 * @return 引擎实例句柄
 */
CHAINTIC_SDK_API chaintic_wake_engine_t chaintic_wake_create_engine(const chaintic_wake_config_t* cfg);
/**
 * @brief 销毁唤醒引擎实例
 * @param engine 通过 chaintic_wake_create_engine 创建的引擎实例
 */
CHAINTIC_SDK_API void chaintic_wake_destroy_engine(chaintic_wake_engine_t engine);
/**
 * @brief 初始化唤醒引擎
 * @param engine 引擎实例
 * @return 0：成功，非 0：失败
 */
CHAINTIC_SDK_API int chaintic_wake_init(chaintic_wake_engine_t engine);
/**
 * @brief 启动唤醒引擎
 * @param engine 引擎实例
 */
CHAINTIC_SDK_API void chaintic_wake_start(chaintic_wake_engine_t engine);
/**
 * @brief 停止唤醒引擎
 * @param engine 引擎实例
 */
CHAINTIC_SDK_API void chaintic_wake_stop(chaintic_wake_engine_t engine);
/**
 * @brief 向引擎喂入一帧 PCM 数据
 * @param engine 引擎实例
 * @param data PCM 数据指针
 * @param samples PCM 采样点数量
 */
CHAINTIC_SDK_API void chaintic_wake_feed(chaintic_wake_engine_t engine, const int16_t* data, size_t samples);
/**
 * @brief 获取一次调用 chaintic_wake_feed 所需的采样点数
 * @param engine 引擎实例
 * @return 采样点数量
 */
CHAINTIC_SDK_API size_t chaintic_wake_get_feed_size(chaintic_wake_engine_t engine);
/**
 * @brief 添加唤醒命令词
 * @param engine 引擎实例
 * @param command 命令词文本（如拼音）
 * @param text 展示文本（如中文）
 */
CHAINTIC_SDK_API void chaintic_wake_add_wake_command(chaintic_wake_engine_t engine, const char* command, const char* text);
/**
 * @brief 添加打断命令词
 * @param engine 引擎实例
 * @param command 命令词文本（如拼音）
 * @param text 展示文本（如中文）
 */
CHAINTIC_SDK_API void chaintic_wake_add_break_command(chaintic_wake_engine_t engine, const char* command, const char* text);
/**
 * @brief 清除所有已添加的命令词
 * @param engine 引擎实例
 */
CHAINTIC_SDK_API void chaintic_wake_clear_commands(chaintic_wake_engine_t engine);
/**
 * @brief 移除指定文本的命令词
 * @param engine 引擎实例
 * @param wake_word 要移除的命令文本
 */
CHAINTIC_SDK_API void chaintic_wake_remove_command(chaintic_wake_engine_t engine, const char* wake_word);

#ifdef __cplusplus
}
#endif

#endif
