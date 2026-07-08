#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "config.h"
#include "box_audio_codec.h"
#include "custom_wake_word.h"
#include "audrec.h"
#include "esp32_s3_szp.h"
#include "driver/i2c_master.h"

static const char *TAG = "AUDREC";

// 全局变量
BoxAudioCodec *g_audio_codec = NULL;
CustomWakeWord *g_wake_word = NULL;
TaskHandle_t g_audio_task_handle = NULL;

// 重采样器类
class Resampler {
public:
    void Configure(int src_rate, int dst_rate) {
        src_rate_ = src_rate;
        dst_rate_ = dst_rate;
    }
    
    size_t GetOutputSamples(size_t src_samples) {
        return (src_samples * dst_rate_) / src_rate_;
    }
    
    void Process(const int16_t* src, size_t src_len, int16_t* dst) {
        // 简单的线性插值重采样实现
        for (size_t i = 0; i < GetOutputSamples(src_len); i++) {
            float src_idx = (float)i * src_rate_ / dst_rate_;
            size_t idx = (size_t)src_idx;
            if (idx < src_len - 1) {
                float frac = src_idx - idx;
                dst[i] = (int16_t)(src[idx] * (1.0f - frac) + src[idx + 1] * frac);
            } else if (idx < src_len) {
                dst[i] = src[idx];
            } else {
                dst[i] = 0;
            }
        }
    }

private:
    int src_rate_ = 0;
    int dst_rate_ = 0;
};

// 输入音频重采样器
Resampler input_resampler_;

// 命令词识别结果回调函数
static void (*g_cmd_cb)(const char *cmd) = NULL;



// 唤醒词检测回调函数
static void wake_word_detected_callback(const std::string& wake_word) {
    ESP_LOGI(TAG, "Wake word detected: %s", wake_word.c_str());
    
    // 如果设置了命令词回调函数，将唤醒词作为命令词传递
    if (g_cmd_cb) {
        g_cmd_cb(wake_word.c_str());
    }
}

/**
 * @brief 初始化音频录制和唤醒词检测
 * 
 * @param cmd_cb 命令词识别结果回调函数
 * @return esp_err_t 
 */
esp_err_t audrec_init(void (*cmd_cb)(const char *cmd)) {
    ESP_LOGI(TAG, "Initializing audio recording and wake word detection");
    
    // 保存命令词回调函数
    g_cmd_cb = cmd_cb;
    
    // 打开功率放大器
    esp_err_t ret = pca9557_enable_pa();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to enable PA: %d", ret);
        // 尝试直接使用GPIO控制PA
        ESP_LOGI(TAG, "Trying alternative PA control method");
        gpio_config_t io_conf = {
            .pin_bit_mask = (1ULL << GPIO_NUM_38),
            .mode = GPIO_MODE_OUTPUT,
            .pull_up_en = GPIO_PULLUP_DISABLE,
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type = GPIO_INTR_DISABLE
        };
        ret = gpio_config(&io_conf);
        if (ret == ESP_OK) {
            ESP_LOGI(TAG, "Configured GPIO38 as output for PA control");
            gpio_set_level(GPIO_NUM_38, 1);
            ESP_LOGI(TAG, "Set GPIO38 to high level to enable PA");
        } else {
            ESP_LOGE(TAG, "Failed to configure GPIO38: %d", ret);
        }
    } else {
        ESP_LOGI(TAG, "PA enabled successfully");
    }
    
    // 初始化音频编解码器
    g_audio_codec = new BoxAudioCodec(
        bsp_i2c_bus_handle,
        AUDIO_INPUT_SAMPLE_RATE,
        AUDIO_OUTPUT_SAMPLE_RATE,
        AUDIO_I2S_GPIO_MCLK,
        AUDIO_I2S_GPIO_BCLK,
        AUDIO_I2S_GPIO_WS,
        AUDIO_I2S_GPIO_DOUT,
        AUDIO_I2S_GPIO_DIN,
        GPIO_NUM_NC,
        AUDIO_CODEC_ES8311_ADDR,
        AUDIO_CODEC_ES7210_ADDR,
        AUDIO_INPUT_REFERENCE
    );
    
    if (!g_audio_codec) {
        ESP_LOGE(TAG, "Failed to create audio codec");
        return ESP_FAIL;
    }
    
    // 初始化唤醒词检测
    g_wake_word = new CustomWakeWord();
    if (!g_wake_word) {
        ESP_LOGE(TAG, "Failed to create wake word detector");
        delete g_audio_codec;
        g_audio_codec = NULL;
        return ESP_FAIL;
    }
    
    // 初始化唤醒词检测
    if (!g_wake_word->Initialize(g_audio_codec, NULL)) {
        ESP_LOGE(TAG, "Failed to initialize wake word detector");
        delete g_wake_word;
        delete g_audio_codec;
        g_wake_word = NULL;
        g_audio_codec = NULL;
        return ESP_FAIL;
    }
    
    // 设置唤醒词检测回调函数
    g_wake_word->OnWakeWordDetected(wake_word_detected_callback);
    
    // 启用音频输入
    g_audio_codec->EnableInput(true);
    
    // 启用音频输出
    g_audio_codec->EnableOutput(true);
    
    // 设置音量为90%
    g_audio_codec->SetOutputVolume(90);
    
    // 启动唤醒词检测
    g_wake_word->Start();
    
    ESP_LOGI(TAG, "Audio recording and wake word detection initialized successfully");
    return ESP_OK;
}

/**
 * @brief 读取音频数据并进行处理
 * 
 * @param data 输出音频数据
 * @param sample_rate 目标采样率
 * @param samples 目标样本数
 * @return true 读取成功
 * @return false 读取失败
 */
static bool ReadAudioData(std::vector<int16_t>& data, int sample_rate, int samples) {
    if (!g_audio_codec->input_enabled()) {
        g_audio_codec->EnableInput(true);
    }

    bool result = false;
    if (g_audio_codec->input_sample_rate() != sample_rate) {
        // 需要重采样
        int src_samples = (samples * g_audio_codec->input_sample_rate()) / sample_rate;
        data.resize(src_samples * g_audio_codec->input_channels());
        
        if (g_audio_codec->InputData(data)) {
            // 如果是立体声，提取左声道
            if (g_audio_codec->input_channels() == 2) {
                auto mono_data = std::vector<int16_t>(data.size() / 2);
                for (size_t i = 0, j = 0; i < mono_data.size(); ++i, j += 2) {
                    mono_data[i] = data[j];
                }
                data = std::move(mono_data);
            }
            
            // 重采样到目标采样率
            size_t dst_samples = input_resampler_.GetOutputSamples(data.size());
            std::vector<int16_t> resampled_data(dst_samples);
            input_resampler_.Process(data.data(), data.size(), resampled_data.data());
            data = std::move(resampled_data);
            result = true;
        }
    } else {
        // 不需要重采样
        data.resize(samples * g_audio_codec->input_channels());
        if (g_audio_codec->InputData(data)) {
            // 如果是立体声，提取左声道
            if (g_audio_codec->input_channels() == 2) {
                auto mono_data = std::vector<int16_t>(data.size() / 2);
                for (size_t i = 0, j = 0; i < mono_data.size(); ++i, j += 2) {
                    mono_data[i] = data[j];
                }
                data = std::move(mono_data);
            }
            result = true;
        }
    }
    
    return result;
}

/**
 * @brief 音频处理任务函数，持续读取音频数据并喂给唤醒词检测器
 * 
 * @param arg 
 */
static void audio_processing_task(void *arg) {
    ESP_LOGI(TAG, "Audio processing task started");
    
    // 获取唤醒词检测器需要的音频块大小
    size_t feed_size = g_wake_word->GetFeedSize();
    if (feed_size == 0) {
        ESP_LOGE(TAG, "Invalid feed size from wake word detector");
        vTaskDelete(NULL);
        return;
    }
    
    // 配置重采样器
    if (g_audio_codec->input_sample_rate() != 16000) {
        input_resampler_.Configure(g_audio_codec->input_sample_rate(), 16000);
        ESP_LOGI(TAG, "Configured resampler: %d -> 16000", g_audio_codec->input_sample_rate());
    }
    
    // 持续读取音频数据并喂给唤醒词检测器
    while (1) {
        // 读取音频数据
        std::vector<int16_t> audio_vec;
        if (ReadAudioData(audio_vec, 16000, feed_size)) {
            // 将音频数据喂给唤醒词检测器
            g_wake_word->Feed(audio_vec);
        }
        
        // 短暂延时，让出CPU
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    
    // 释放资源（实际上不会执行到这里）
    vTaskDelete(NULL);
}

/**
 * @brief 启动音频录制和唤醒词检测任务
 * 
 * @return esp_err_t 
 */
esp_err_t audrec_start(void) {
    ESP_LOGI(TAG, "Starting audio recording and wake word detection task");
    
    // 创建音频处理任务
    BaseType_t ret = xTaskCreatePinnedToCore(
        audio_processing_task,
        "audio_processing",
        4096,
        NULL,
        5,
        &g_audio_task_handle,
        0
    );
    
    if (ret != pdPASS) {
        ESP_LOGE(TAG, "Failed to create audio processing task");
        return ESP_FAIL;
    }
    
    return ESP_OK;
}

/**
 * @brief 停止音频录制和唤醒词检测
 * 
 * @return esp_err_t 
 */
esp_err_t audrec_stop(void) {
    ESP_LOGI(TAG, "Stopping audio recording and wake word detection");
    
    // 停止唤醒词检测
    if (g_wake_word) {
        g_wake_word->Stop();
    }
    
    // 停止音频处理任务
    if (g_audio_task_handle) {
        vTaskDelete(g_audio_task_handle);
        g_audio_task_handle = NULL;
    }
    
    // 禁用音频输入输出
    if (g_audio_codec) {
        g_audio_codec->EnableInput(false);
        g_audio_codec->EnableOutput(false);
    }
    
    return ESP_OK;
}

/**
 * @brief 添加唤醒词命令
 * 
 * @param command 唤醒词命令
 * @param command_name 唤醒词显示名称
 * @return esp_err_t 
 */
esp_err_t audrec_add_command(const char *command, const char *command_name) {
    ESP_LOGI(TAG, "Adding wake word command: %s, name: %s", command, command_name);
    
    if (g_wake_word == NULL) {
        ESP_LOGE(TAG, "Wake word detector not initialized");
        return ESP_FAIL;
    }
    
    if (command == NULL || command_name == NULL) {
        ESP_LOGE(TAG, "Invalid command or command name");
        return ESP_FAIL;
    }
    
    // 调用CustomWakeWord的AddWNCommand方法添加唤醒词
    g_wake_word->AddWNCommand(command, command_name);
    
    return ESP_OK;
}

/**
 * @brief 将PCM数据输出到I2S
 * 
 * @param pcm PCM数据
 * @param samples 样本数量
 * @return esp_err_t 
 */
esp_err_t audrec_output_pcm(const int16_t* pcm, int samples) {
    ESP_LOGD(TAG, "Outputting %d PCM samples to I2S", samples);
    
    if (g_audio_codec == NULL) {
        ESP_LOGE(TAG, "Audio codec not initialized");
        return ESP_FAIL;
    }
    
    if (pcm == NULL || samples <= 0) {
        ESP_LOGE(TAG, "Invalid PCM data or sample count");
        return ESP_FAIL;
    }
    
    // 确保音频输出已启用
    if (!g_audio_codec->output_enabled()) {
        g_audio_codec->EnableOutput(true);
    }
    
    // 使用AudioCodec的Write方法输出PCM数据
    g_audio_codec->Write(pcm, samples);
    
    return ESP_OK;
}

/**
 * @brief 反初始化音频录制和唤醒词检测
 * 
 * @return esp_err_t 
 */
esp_err_t audrec_deinit(void) {
    ESP_LOGI(TAG, "Deinitializing audio recording and wake word detection");
    
    // 停止音频录制和唤醒词检测
    audrec_stop();
    
    // 关闭功率放大器
    pca9557_disable_pa();
    
    // 释放资源
    if (g_wake_word) {
        delete g_wake_word;
        g_wake_word = NULL;
    }
    
    if (g_audio_codec) {
        delete g_audio_codec;
        g_audio_codec = NULL;
    }
    
    g_cmd_cb = NULL;
    
    return ESP_OK;
}
