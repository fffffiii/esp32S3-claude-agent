/*
 * audio_service.c - 语音录制 + 唤醒词检测 + 扬声器播放
 *
 * ES8388 编解码器通过 esp_codec_dev 管理
 * 唤醒词检测通过 wake_engine (直接 multinet API，无 AFE)
 * 数据流: I2S stereo(ch0=AEC参考, ch1=麦克风) → 提取麦克风声道 mono → wake_engine_feed → multinet detect → callback
 */

#include "audio_service.h"
#include "config.h"
#include "wake_engine.h"

#include <string.h>
#include <stdlib.h>
#include <limits.h>
#include <stdbool.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "driver/gpio.h"

#define TAG "AudioService"

#define AUDIO_CODEC_DMA_DESC_NUM   6
#define AUDIO_CODEC_DMA_FRAME_NUM  240
#define AUDIO_INPUT_GAIN_DB        24.0f
#define AUDIO_INPUT_LOG_INTERVAL_US 1000000
#define AUDIO_INPUT_SCAN_INTERVAL_US 3000000
#define AUDIO_INPUT_SILENCE_PEAK   300
#define AUDIO_INPUT_REF_CHANNEL    0 /* ch0 是 AEC 参考 */
#define AUDIO_INPUT_MIC_CHANNEL    1 /* ch1 是麦克风 */

#ifndef AUDIO_INPUT_AUTO_WAKE_CHANNEL
#define AUDIO_INPUT_AUTO_WAKE_CHANNEL 0 /* 1=调试时自动选择有声声道 */
#endif

#ifndef AUDIO_CODEC_ES8388_ADC_INPUT
#define AUDIO_CODEC_ES8388_ADC_INPUT 0xf0 /* 0xf0+0x82=diff2，-1=自动扫描 */
#endif

#ifndef AUDIO_CODEC_ES8388_ADC_INPUT_CONTROL3
#define AUDIO_CODEC_ES8388_ADC_INPUT_CONTROL3 0x82 /* diff1/diff2 的 adc2 相同，用 adc3 固定 diff2 */
#endif

typedef struct {
    uint32_t peak;
    uint32_t avg_abs;
} audio_frame_stats_t;

typedef struct {
    uint8_t adc_control2;
    uint8_t adc_control3;
    const char *name;
} es8388_input_path_t;

/* ------------------------------------------------------------------ */
/*  全局状态                                                          */
/* ------------------------------------------------------------------ */
static i2c_master_bus_handle_t s_i2c_bus    = NULL;
static i2s_chan_handle_t       s_tx_handle  = NULL;
static i2s_chan_handle_t       s_rx_handle  = NULL;

static const audio_codec_data_if_t  *s_data_if   = NULL;
static const audio_codec_ctrl_if_t  *s_ctrl_if   = NULL;
static const audio_codec_if_t       *s_codec_if  = NULL;
static const audio_codec_gpio_if_t  *s_gpio_if   = NULL;
static esp_codec_dev_handle_t       s_output_dev = NULL;
static esp_codec_dev_handle_t       s_input_dev  = NULL;

static wake_engine_t                s_engine     = NULL;
static TaskHandle_t                 s_audio_task = NULL;
static audio_service_wake_cb_t      s_wake_cb    = NULL;
static SemaphoreHandle_t            s_playback_mutex = NULL;
static int                          s_volume = 70; /* 默认音量，会被 NVS 值覆盖 */

static const es8388_input_path_t s_input_paths[] = {
    {0x50, 0x02, "line2"},
    {0x05, 0x02, "mic1"},
    {0x06, 0x02, "mic2"},
    {0x00, 0x02, "line1"},
    {0xf0, 0x02, "diff1"},
    {0xf0, 0x82, "diff2"},
};
static int s_input_path_index = 0;

static int input_path_count(void)
{
    return (int)(sizeof(s_input_paths) / sizeof(s_input_paths[0]));
}

static inline uint32_t sample_abs16(int16_t value)
{
    return value == INT16_MIN ? 32768U : (uint32_t)(value < 0 ? -value : value);
}

static audio_frame_stats_t calc_interleaved_stats(const int16_t *data,
                                                  size_t frames,
                                                  int channels,
                                                  int channel)
{
    audio_frame_stats_t stats = {0};
    uint64_t sum = 0;

    if (!data || frames == 0 || channels <= 0 || channel < 0 || channel >= channels) {
        return stats;
    }

    for (size_t i = 0; i < frames; i++) {
        uint32_t value = sample_abs16(data[i * channels + channel]);
        if (value > stats.peak) {
            stats.peak = value;
        }
        sum += value;
    }
    stats.avg_abs = (uint32_t)(sum / frames);
    return stats;
}

static void copy_interleaved_channel(const int16_t *src,
                                     int16_t *dst,
                                     size_t frames,
                                     int channels,
                                     int channel)
{
    if (!src || !dst || channels <= 0 || channel < 0 || channel >= channels) {
        return;
    }
    for (size_t i = 0; i < frames; i++) {
        dst[i] = src[i * channels + channel];
    }
}

static void apply_es8388_input_path(int index)
{
    if (!s_ctrl_if || index < 0 || index >= input_path_count()) {
        return;
    }

    const es8388_input_path_t *path = &s_input_paths[index];
    uint8_t gain_step = (uint8_t)(AUDIO_INPUT_GAIN_DB / 3.0f);
    if (gain_step > 8) {
        gain_step = 8;
    }
    uint8_t gain = (uint8_t)((gain_step << 4) | gain_step);
    uint8_t adc_power = 0x00;
    uint8_t adc_unmute = 0x30;
    uint8_t adc_volume = 0x00;
    uint8_t chip_power = 0x00;

    /* 打开 ADC、模拟输入和 MICBIAS；ADF 默认 0x09 会关闭 MICBIAS。 */
    uint8_t adc_control2 = path->adc_control2;
    uint8_t adc_control3 = path->adc_control3;

    s_ctrl_if->write_reg(s_ctrl_if, 0x03, 1, &adc_power, 1);
    s_ctrl_if->write_reg(s_ctrl_if, 0x09, 1, &gain, 1);
    s_ctrl_if->write_reg(s_ctrl_if, 0x0a, 1, &adc_control2, 1);
    s_ctrl_if->write_reg(s_ctrl_if, 0x0b, 1, &adc_control3, 1);
    s_ctrl_if->write_reg(s_ctrl_if, 0x0f, 1, &adc_unmute, 1);
    s_ctrl_if->write_reg(s_ctrl_if, 0x10, 1, &adc_volume, 1);
    s_ctrl_if->write_reg(s_ctrl_if, 0x11, 1, &adc_volume, 1);
    s_ctrl_if->write_reg(s_ctrl_if, 0x02, 1, &chip_power, 1);

    ESP_LOGI(TAG, "ES8388 input path: %s adc2=0x%02x adc3=0x%02x gain=%.1fdB micbias=on adc_unmute=0x%02x",
             path->name, path->adc_control2, path->adc_control3,
             AUDIO_INPUT_GAIN_DB, adc_unmute);
}

static const char *current_input_path_name(void)
{
    if (s_input_path_index < 0 || s_input_path_index >= input_path_count()) {
        return "unknown";
    }
    return s_input_paths[s_input_path_index].name;
}

static int select_wake_channel(int channels,
                               audio_frame_stats_t ch0_stats,
                               audio_frame_stats_t ch1_stats)
{
    if (channels < 2) {
        return 0;
    }

#if AUDIO_INPUT_AUTO_WAKE_CHANNEL
    /* 调试阶段防止 I2S 左右顺序和原理图标注相反：右声道静音但左声道有明显信号时，先把有声通道喂给 MultiNet。 */
    if (ch1_stats.peak < AUDIO_INPUT_SILENCE_PEAK && ch0_stats.peak >= AUDIO_INPUT_SILENCE_PEAK) {
        return AUDIO_INPUT_REF_CHANNEL;
    }
#endif

    return AUDIO_INPUT_MIC_CHANNEL;
}

/* ------------------------------------------------------------------ */
/*  I2C 总线初始化                                                     */
/* ------------------------------------------------------------------ */
static esp_err_t init_i2c(void)
{
    i2c_master_bus_config_t cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = AUDIO_CODEC_I2C_SDA_PIN,
        .scl_io_num = AUDIO_CODEC_I2C_SCL_PIN,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .intr_priority = 0,
        .trans_queue_depth = 0,
        .flags = {
            .enable_internal_pullup = true,
        },
    };
    return i2c_new_master_bus(&cfg, &s_i2c_bus);
}

/* ------------------------------------------------------------------ */
/*  I2S 双工通道                                                       */
/* ------------------------------------------------------------------ */
static esp_err_t init_i2s(void)
{
    i2s_chan_config_t chan_cfg = {
        .id = I2S_NUM_0,
        .role = I2S_ROLE_MASTER,
        .dma_desc_num = AUDIO_CODEC_DMA_DESC_NUM,
        .dma_frame_num = AUDIO_CODEC_DMA_FRAME_NUM,
        .auto_clear_after_cb = true,
        .auto_clear_before_cb = false,
        .intr_priority = 0,
    };
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &s_tx_handle, &s_rx_handle));

    i2s_std_config_t std_cfg = {
        .clk_cfg = {
            .sample_rate_hz = (uint32_t)AUDIO_OUTPUT_SAMPLE_RATE,
            .clk_src = I2S_CLK_SRC_DEFAULT,
            .ext_clk_freq_hz = 0,
            .mclk_multiple = I2S_MCLK_MULTIPLE_256,
        },
        .slot_cfg = {
            .data_bit_width = I2S_DATA_BIT_WIDTH_16BIT,
            .slot_bit_width = I2S_SLOT_BIT_WIDTH_AUTO,
            .slot_mode = I2S_SLOT_MODE_STEREO,
            .slot_mask = I2S_STD_SLOT_BOTH,
            .ws_width = I2S_DATA_BIT_WIDTH_16BIT,
            .ws_pol = false,
            .bit_shift = true,
            .left_align = true,
            .big_endian = false,
            .bit_order_lsb = false,
        },
        .gpio_cfg = {
            .mclk = AUDIO_I2S_GPIO_MCLK,
            .bclk = AUDIO_I2S_GPIO_BCLK,
            .ws   = AUDIO_I2S_GPIO_WS,
            .dout = AUDIO_I2S_GPIO_DOUT,
            .din  = AUDIO_I2S_GPIO_DIN,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };

    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx_handle, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_rx_handle, &std_cfg));

    ESP_ERROR_CHECK(i2s_channel_enable(s_tx_handle));
    ESP_ERROR_CHECK(i2s_channel_enable(s_rx_handle));

    ESP_LOGI(TAG, "I2S duplex channels created");
    return ESP_OK;
}

/* ------------------------------------------------------------------ */
/*  ES8388 编解码器初始化                                              */
/* ------------------------------------------------------------------ */
static esp_err_t init_es8388(void)
{
    /* data_if: I2S 数据接口 */
    audio_codec_i2s_cfg_t i2s_cfg = {
        .port      = I2S_NUM_0,
        .rx_handle = s_rx_handle,
        .tx_handle = s_tx_handle,
    };
    s_data_if = audio_codec_new_i2s_data(&i2s_cfg);
    if (!s_data_if) { ESP_LOGE(TAG, "Failed to create I2S data if"); return ESP_FAIL; }

    /* ctrl_if: I2C 控制接口 */
    audio_codec_i2c_cfg_t i2c_cfg = {
        .port       = (i2c_port_t)1,
        .addr       = AUDIO_CODEC_ES8388_ADDR,
        .bus_handle = s_i2c_bus,
    };
    s_ctrl_if = audio_codec_new_i2c_ctrl(&i2c_cfg);
    if (!s_ctrl_if) { ESP_LOGE(TAG, "Failed to create I2C ctrl if"); return ESP_FAIL; }

    /* gpio_if */
    s_gpio_if = audio_codec_new_gpio();
    if (!s_gpio_if) { ESP_LOGE(TAG, "Failed to create GPIO if"); return ESP_FAIL; }

    /* codec: ES8388 */
    es8388_codec_cfg_t es8388_cfg = {
        .ctrl_if    = s_ctrl_if,
        .gpio_if    = s_gpio_if,
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_BOTH,
        .master_mode = true,
        .pa_pin     = AUDIO_CODEC_PA_PIN,
        .pa_reverted = false,
        .hw_gain    = {
            .pa_voltage         = 5.0,
            .codec_dac_voltage  = 3.3,
        },
    };
    s_codec_if = es8388_codec_new(&es8388_cfg);
    if (!s_codec_if) { ESP_LOGE(TAG, "Failed to create ES8388 codec"); return ESP_FAIL; }

    /* 输出设备 */
    esp_codec_dev_cfg_t out_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_OUT,
        .codec_if = s_codec_if,
        .data_if  = s_data_if,
    };
    s_output_dev = esp_codec_dev_new(&out_cfg);
    if (!s_output_dev) { ESP_LOGE(TAG, "Failed to create output dev"); return ESP_FAIL; }

    /* 输入设备 */
    esp_codec_dev_cfg_t in_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_IN,
        .codec_if = s_codec_if,
        .data_if  = s_data_if,
    };
    s_input_dev = esp_codec_dev_new(&in_cfg);
    if (!s_input_dev) { ESP_LOGE(TAG, "Failed to create input dev"); return ESP_FAIL; }

    esp_codec_set_disable_when_closed(s_output_dev, false);
    esp_codec_set_disable_when_closed(s_input_dev, false);

    ESP_LOGI(TAG, "ES8388 codec initialized");
    return ESP_OK;
}

/* ------------------------------------------------------------------ */
/*  启用音频输入（ch0=AEC 参考，ch1=麦克风）                           */
/* ------------------------------------------------------------------ */
static void enable_input(void)
{
    esp_codec_dev_sample_info_t fs = {
        .bits_per_sample = 16,
        .channel         = AUDIO_INPUT_REFERENCE ? 2 : 1,
        .channel_mask    = ESP_CODEC_DEV_MAKE_CHANNEL_MASK(0),
        .sample_rate     = (uint32_t)AUDIO_INPUT_SAMPLE_RATE,
        .mclk_multiple   = 0,
    };
    if (AUDIO_INPUT_REFERENCE) {
        fs.channel_mask |= ESP_CODEC_DEV_MAKE_CHANNEL_MASK(1);
    }
    ESP_ERROR_CHECK(esp_codec_dev_open(s_input_dev, &fs));

    if (AUDIO_CODEC_ES8388_ADC_INPUT >= 0) {
        bool found = false;
        int fixed_adc3 = AUDIO_CODEC_ES8388_ADC_INPUT_CONTROL3;
        for (int i = 0; i < input_path_count(); i++) {
            bool adc3_match = fixed_adc3 < 0 || s_input_paths[i].adc_control3 == (uint8_t)fixed_adc3;
            if (s_input_paths[i].adc_control2 == (uint8_t)AUDIO_CODEC_ES8388_ADC_INPUT && adc3_match) {
                s_input_path_index = i;
                found = true;
                break;
            }
        }
        if (!found) {
            ESP_LOGW(TAG, "Configured ES8388 input adc2=0x%02x adc3=0x%02x not found, fallback to %s",
                     (unsigned int)(uint8_t)AUDIO_CODEC_ES8388_ADC_INPUT,
                     (unsigned int)(uint8_t)AUDIO_CODEC_ES8388_ADC_INPUT_CONTROL3,
                     current_input_path_name());
        }
    }
    apply_es8388_input_path(s_input_path_index);
    ESP_LOGI(TAG, "Input enabled (ref=%d adc_input=%s mode=%s)",
             AUDIO_INPUT_REFERENCE,
             current_input_path_name(),
             AUDIO_CODEC_ES8388_ADC_INPUT < 0 ? "auto-scan" : "fixed");
}

/* ------------------------------------------------------------------ */
/*  启用音频输出                                                       */
/* ------------------------------------------------------------------ */
static void enable_output(void)
{
    esp_codec_dev_sample_info_t fs = {
        .bits_per_sample = 16,
        .channel         = 1,
        .channel_mask    = 0,
        .sample_rate     = (uint32_t)AUDIO_OUTPUT_SAMPLE_RATE,
        .mclk_multiple   = 0,
    };
    ESP_ERROR_CHECK(esp_codec_dev_open(s_output_dev, &fs));
    ESP_ERROR_CHECK(esp_codec_dev_set_out_vol(s_output_dev, s_volume));

    uint8_t reg_val = AUDIO_INPUT_REFERENCE ? 27 : 30;
    uint8_t regs[] = { 46, 47, 48, 49 };
    for (int i = 0; i < 4; i++) {
        s_ctrl_if->write_reg(s_ctrl_if, regs[i], 1, &reg_val, 1);
    }

    if (AUDIO_CODEC_PA_PIN != GPIO_NUM_NC) {
        gpio_set_level(AUDIO_CODEC_PA_PIN, 1);
    }
    ESP_LOGI(TAG, "Output enabled");
}

/* ------------------------------------------------------------------ */
/*  音频处理任务                                                       */
/*  I2S stereo → 提取麦克风声道 → wake_engine (multinet detect)          */
/* ------------------------------------------------------------------ */
static void audio_task(void *arg)
{
    ESP_LOGI(TAG, "Audio task started");

    size_t feed_size = wake_engine_get_feed_size(s_engine);
    if (feed_size == 0) {
        feed_size = 512; /* multinet 默认 chunksize */
    }

    ESP_LOGI(TAG, "Feed size: %d samples @ %dHz (mono)", feed_size, AUDIO_INPUT_SAMPLE_RATE);

    int channels = AUDIO_INPUT_REFERENCE ? 2 : 1;

    int16_t *stereo_buf = (int16_t *)malloc(feed_size * channels * sizeof(int16_t));
    int16_t *mono_buf   = (int16_t *)malloc(feed_size * sizeof(int16_t));
    if (!stereo_buf || !mono_buf) {
        ESP_LOGE(TAG, "Failed to alloc audio buffers");
        vTaskDelete(NULL);
        return;
    }

    wake_engine_start(s_engine);

    int64_t last_log_us = esp_timer_get_time();
    int64_t last_input_scan_us = esp_timer_get_time();
    int selected_channel = (channels > AUDIO_INPUT_MIC_CHANNEL) ? AUDIO_INPUT_MIC_CHANNEL : 0;
    ESP_LOGI(TAG, "Audio input mapping: ch%d=ref/aec, ch%d=mic, preferred multinet ch%d, adc path mode=%s",
             AUDIO_INPUT_REF_CHANNEL,
             AUDIO_INPUT_MIC_CHANNEL,
             selected_channel,
             AUDIO_CODEC_ES8388_ADC_INPUT < 0 ? "auto-scan" : "fixed");

    while (1) {
        /* 从 ES8388 读取一帧 PCM（立体声交错输入） */
        esp_err_t r = esp_codec_dev_read(s_input_dev, stereo_buf,
                                         feed_size * channels * sizeof(int16_t));
        if (r != ESP_OK) {
            ESP_LOGW(TAG, "esp_codec_dev_read failed: %d", r);
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }

        audio_frame_stats_t ch0_stats = calc_interleaved_stats(stereo_buf, feed_size, channels, AUDIO_INPUT_REF_CHANNEL);
        audio_frame_stats_t ch1_stats = {0};

        if (channels == 2) {
            ch1_stats = calc_interleaved_stats(stereo_buf, feed_size, channels, AUDIO_INPUT_MIC_CHANNEL);
            selected_channel = select_wake_channel(channels, ch0_stats, ch1_stats);
            copy_interleaved_channel(stereo_buf, mono_buf, feed_size, channels, selected_channel);
        } else {
            memcpy(mono_buf, stereo_buf, feed_size * sizeof(int16_t));
            selected_channel = 0;
        }
        audio_frame_stats_t mono_stats = calc_interleaved_stats(mono_buf, feed_size, 1, 0);

        /* 打印输入能量，方便判断麦克风是否真的收到人声。 */
        int64_t now = esp_timer_get_time();

        if (AUDIO_CODEC_ES8388_ADC_INPUT < 0) {
            if (mono_stats.peak >= AUDIO_INPUT_SILENCE_PEAK) {
                last_input_scan_us = now;
            } else if (now - last_input_scan_us > AUDIO_INPUT_SCAN_INTERVAL_US) {
                int old_index = s_input_path_index;
                s_input_path_index = (s_input_path_index + 1) % input_path_count();
                ESP_LOGW(TAG, "Mic level low on path=%s peak=%lu, scan ES8388 input path -> %s",
                         s_input_paths[old_index].name,
                         (unsigned long)mono_stats.peak,
                         current_input_path_name());
                apply_es8388_input_path(s_input_path_index);
                last_input_scan_us = now;
            }
        }

        /* 16kHz mono 数据直接喂给 multinet */
        wake_engine_feed(s_engine, mono_buf, feed_size);
    }

    free(stereo_buf);
    free(mono_buf);
    vTaskDelete(NULL);
}

/* ------------------------------------------------------------------ */
/*  公共 API                                                          */
/* ------------------------------------------------------------------ */
void audio_service_set_volume_init(int volume)
{
    if (volume < 0) volume = 0;
    if (volume > 100) volume = 100;
    s_volume = volume;
}

esp_err_t audio_service_init(audio_service_wake_cb_t wake_cb)
{
    ESP_LOGI(TAG, "Initializing audio service");

    s_wake_cb = wake_cb;

    if (!s_playback_mutex) {
        s_playback_mutex = xSemaphoreCreateMutex();
        if (!s_playback_mutex) {
            ESP_LOGE(TAG, "Failed to create playback mutex");
            return ESP_FAIL;
        }
    }

    /* PA 引脚配置 */
    if (AUDIO_CODEC_PA_PIN != GPIO_NUM_NC) {
        gpio_config_t pa_conf = {
            .pin_bit_mask = (1ULL << AUDIO_CODEC_PA_PIN),
            .mode         = GPIO_MODE_OUTPUT,
            .pull_up_en   = GPIO_PULLUP_DISABLE,
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type    = GPIO_INTR_DISABLE,
        };
        ESP_ERROR_CHECK(gpio_config(&pa_conf));
        gpio_set_level(AUDIO_CODEC_PA_PIN, 0);
    }

    ESP_ERROR_CHECK(init_i2c());
    ESP_ERROR_CHECK(init_i2s());
    ESP_ERROR_CHECK(init_es8388());

    enable_input();
    enable_output();

    /* 唤醒引擎: 直接用 multinet，无 AFE */
    s_engine = wake_engine_create("cn", 3000, 0.05f);
    if (!s_engine) {
        ESP_LOGE(TAG, "Failed to create wake engine");
        return ESP_FAIL;
    }

    if (wake_engine_init(s_engine) != 0) {
        ESP_LOGE(TAG, "Failed to init wake engine. Rebooting in 3s...");
        wake_engine_destroy(s_engine);
        s_engine = NULL;
        vTaskDelay(pdMS_TO_TICKS(3000));
        esp_restart();
    }

    /* 设置回调 */
    wake_engine_set_callback(s_engine, s_wake_cb);

    /* 添加命令词 */
    wake_engine_clear_commands(s_engine);
    /* 唤醒提示 */
    wake_engine_add_command(s_engine, "xiao ke", "小可");
    wake_engine_add_command(s_engine, "xiao ke xiao ke", "小可");
    /* 动作命令: 继续/确定/拒绝 */
    wake_engine_add_command(s_engine, "ji xu", "继续");
    wake_engine_add_command(s_engine, "que ding", "确定");
    wake_engine_add_command(s_engine, "ju jue", "拒绝");
    /* 音量命令 */
    wake_engine_add_command(s_engine, "tiao da yin liang", "调大音量");
    wake_engine_add_command(s_engine, "tiao xiao yin liang", "调小音量");

    ESP_LOGI(TAG, "Audio service initialized (commands: 小可/继续/确定/拒绝/调大音量/调小音量)");
    return ESP_OK;
}

esp_err_t audio_service_start(void)
{
    ESP_LOGI(TAG, "Starting audio service");

    BaseType_t ret = xTaskCreatePinnedToCore(
        audio_task,
        "audio_svc",
        4096,
        NULL,
        5,
        &s_audio_task,
        0  /* 音频任务跑在 core 0 */
    );

    if (ret != pdPASS) {
        ESP_LOGE(TAG, "Failed to create audio task");
        return ESP_FAIL;
    }

    return ESP_OK;
}

esp_err_t audio_service_stop(void)
{
    ESP_LOGI(TAG, "Stopping audio service");

    if (s_engine) {
        wake_engine_stop(s_engine);
    }

    if (s_audio_task) {
        vTaskDelete(s_audio_task);
        s_audio_task = NULL;
    }

    return ESP_OK;
}

esp_err_t audio_service_output_pcm(const int16_t *pcm, int samples)
{
    if (!s_output_dev || !pcm || samples <= 0) {
        return ESP_ERR_INVALID_ARG;
    }
    return esp_codec_dev_write(s_output_dev, (void *)pcm, samples * sizeof(int16_t));
}

esp_err_t audio_service_playback_acquire(uint32_t timeout_ms)
{
    if (!s_playback_mutex) {
        return ESP_OK;
    }
    TickType_t ticks = timeout_ms == portMAX_DELAY ? portMAX_DELAY : pdMS_TO_TICKS(timeout_ms);
    return xSemaphoreTake(s_playback_mutex, ticks) == pdTRUE ? ESP_OK : ESP_ERR_TIMEOUT;
}

void audio_service_playback_release(void)
{
    if (s_playback_mutex) {
        xSemaphoreGive(s_playback_mutex);
    }
}

void audio_service_set_volume(int volume)
{
    if (volume < 0) volume = 0;
    if (volume > 100) volume = 100;
    s_volume = volume;
    if (s_output_dev) {
        esp_codec_dev_set_out_vol(s_output_dev, s_volume);
    }
    ESP_LOGI(TAG, "Volume set to %d", s_volume);
}

int audio_service_get_volume(void)
{
    return s_volume;
}

esp_err_t audio_service_deinit(void)
{
    audio_service_stop();

    if (s_engine) {
        wake_engine_destroy(s_engine);
        s_engine = NULL;
    }

    if (s_output_dev) { esp_codec_dev_close(s_output_dev); esp_codec_dev_delete(s_output_dev); s_output_dev = NULL; }
    if (s_input_dev)  { esp_codec_dev_close(s_input_dev);  esp_codec_dev_delete(s_input_dev);  s_input_dev  = NULL; }
    if (s_codec_if)   { audio_codec_delete_codec_if(s_codec_if);   s_codec_if  = NULL; }
    if (s_ctrl_if)    { audio_codec_delete_ctrl_if(s_ctrl_if);     s_ctrl_if   = NULL; }
    if (s_gpio_if)    { audio_codec_delete_gpio_if(s_gpio_if);     s_gpio_if   = NULL; }
    if (s_data_if)    { audio_codec_delete_data_if(s_data_if);     s_data_if   = NULL; }

    if (s_tx_handle) { i2s_channel_disable(s_tx_handle); i2s_del_channel(s_tx_handle); s_tx_handle = NULL; }
    if (s_rx_handle) { i2s_channel_disable(s_rx_handle); i2s_del_channel(s_rx_handle); s_rx_handle = NULL; }
    if (s_i2c_bus)   { i2c_del_master_bus(s_i2c_bus);    s_i2c_bus   = NULL; }
    if (s_playback_mutex) { vSemaphoreDelete(s_playback_mutex); s_playback_mutex = NULL; }

    s_wake_cb = NULL;
    ESP_LOGI(TAG, "Audio service deinitialized");
    return ESP_OK;
}
