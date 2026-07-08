/*
 * task_sound.c - 提示音播放模块
 *
 * ADF 播放链路: spiffs_stream -> mp3_decoder -> rsp_filter -> raw_stream
 * 后台任务从 raw_stream 读取 PCM，写入 audio_service_output_pcm()
 */

#include "task_sound.h"
#include "audio_service.h"
#include "config.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_err.h"
#include "esp_log.h"
#include <string.h>
#include <stdlib.h>
#include <stdbool.h>

#include "audio_pipeline.h"
#include "audio_element.h"
#include "spiffs_stream.h"
#include "raw_stream.h"
#include "filter_resample.h"
#include "mp3_decoder.h"

#define TAG "TaskSound"

#define PLAYBACK_TASK_STACK  (8 * 1024)
#define PLAYBACK_TASK_PRIO   4
#define PLAYBACK_TASK_CORE   1
#define PCM_READ_BYTES       1024

typedef struct {
    audio_pipeline_handle_t pipeline;
    audio_element_handle_t  spiffs_reader;
    audio_element_handle_t  mp3_dec;
    audio_element_handle_t  resampler;
    audio_element_handle_t  raw_out;
    bool                    registered;
} sound_pipeline_t;

static const char *s_file_uris[] = {
    [TASK_SOUND_DONE]            = "/spiffs/task_done.mp3",
    [TASK_SOUND_PERMISSION_WAIT] = "/spiffs/permission_wait.mp3",
    [TASK_SOUND_ACTION_CONFIRM]  = "/spiffs/action_confirm.mp3",
};

static TaskHandle_t     s_play_task    = NULL;
static QueueHandle_t    s_play_queue   = NULL;
static volatile bool    s_playing      = false;
static volatile bool    s_stop_requested = false;
static volatile audio_pipeline_handle_t s_active_pipeline = NULL;

static void set_active_pipeline(audio_pipeline_handle_t pipeline)
{
    s_active_pipeline = pipeline;
}

static void clear_active_pipeline(audio_pipeline_handle_t pipeline)
{
    if (s_active_pipeline == pipeline) {
        s_active_pipeline = NULL;
    }
}

/* ── Pipeline 生命周期 ── */

static void pipeline_release(sound_pipeline_t *p)
{
    esp_log_level_t old_element_log = esp_log_level_get("AUDIO_ELEMENT");
    esp_log_level_t old_pipeline_log = esp_log_level_get("AUDIO_PIPELINE");

    /*
     * ADF 在 EOF 后会把 element 任务自然销毁，deinit 再发 terminate 会打印
     * "Element has not create" 警告。这里短暂压低 ADF 清理日志，保留真实错误。
     */
    esp_log_level_set("AUDIO_ELEMENT", ESP_LOG_ERROR);
    esp_log_level_set("AUDIO_PIPELINE", ESP_LOG_ERROR);

    if (p->pipeline) {
        audio_pipeline_deinit(p->pipeline);
    }

    if (!p->registered) {
        if (p->spiffs_reader) audio_element_deinit(p->spiffs_reader);
        if (p->mp3_dec)       audio_element_deinit(p->mp3_dec);
        if (p->resampler)     audio_element_deinit(p->resampler);
        if (p->raw_out)       audio_element_deinit(p->raw_out);
    }

    esp_log_level_set("AUDIO_ELEMENT", old_element_log);
    esp_log_level_set("AUDIO_PIPELINE", old_pipeline_log);
    memset(p, 0, sizeof(*p));
}

static esp_err_t pipeline_create(sound_pipeline_t *p)
{
    memset(p, 0, sizeof(*p));

    /* SPIFFS 文件读取器 */
    spiffs_stream_cfg_t spiffs_cfg = SPIFFS_STREAM_CFG_DEFAULT();
    spiffs_cfg.type = AUDIO_STREAM_READER;
    p->spiffs_reader = spiffs_stream_init(&spiffs_cfg);

    /* MP3 解码器 */
    mp3_decoder_cfg_t mp3_cfg = DEFAULT_MP3_DECODER_CONFIG();
    p->mp3_dec = mp3_decoder_init(&mp3_cfg);

    /* 重采样器：当前提示音是 44.1kHz mono MP3，输出转为板级播放采样率。 */
    rsp_filter_cfg_t rsp_cfg = DEFAULT_RESAMPLE_FILTER_CONFIG();
    rsp_cfg.src_rate   = 44100;
    rsp_cfg.src_ch     = 1;
    rsp_cfg.src_bits   = 16;
    rsp_cfg.dest_rate  = AUDIO_OUTPUT_SAMPLE_RATE;
    rsp_cfg.dest_bits  = 16;
    rsp_cfg.dest_ch    = 1;
    rsp_cfg.mode       = RESAMPLE_DECODE_MODE;
    rsp_cfg.complexity = 1;
    rsp_cfg.stack_in_ext = false;
    p->resampler = rsp_filter_init(&rsp_cfg);

    /* raw stream 作为 PCM 出口，由播放任务主动读取。 */
    raw_stream_cfg_t raw_cfg = RAW_STREAM_CFG_DEFAULT();
    raw_cfg.type = AUDIO_STREAM_READER;
    p->raw_out = raw_stream_init(&raw_cfg);

    if (!p->spiffs_reader || !p->mp3_dec || !p->resampler || !p->raw_out) {
        ESP_LOGE(TAG, "Failed to create pipeline elements");
        pipeline_release(p);
        return ESP_FAIL;
    }

    /* 组装播放链路 */
    audio_pipeline_cfg_t pipe_cfg = DEFAULT_AUDIO_PIPELINE_CONFIG();
    p->pipeline = audio_pipeline_init(&pipe_cfg);
    if (!p->pipeline) {
        ESP_LOGE(TAG, "Failed to init pipeline");
        pipeline_release(p);
        return ESP_FAIL;
    }

    audio_pipeline_register(p->pipeline, p->spiffs_reader, "spiffs");
    audio_pipeline_register(p->pipeline, p->mp3_dec,      "mp3");
    audio_pipeline_register(p->pipeline, p->resampler,     "rsp");
    audio_pipeline_register(p->pipeline, p->raw_out,       "raw");
    p->registered = true;

    const char *link_tag[4] = {"spiffs", "mp3", "rsp", "raw"};
    if (audio_pipeline_link(p->pipeline, &link_tag[0], 4) != ESP_OK) {
        ESP_LOGE(TAG, "Failed to link pipeline");
        pipeline_release(p);
        return ESP_FAIL;
    }

    return ESP_OK;
}

static void pipeline_finish(sound_pipeline_t *p, bool interrupted)
{
    if (p->pipeline) {
        if (interrupted) {
            audio_pipeline_stop(p->pipeline);
        }
        audio_pipeline_wait_for_stop(p->pipeline);
    }
}

/* ── 播放任务 ── */

static void play_one_sound(task_sound_type_t type, uint8_t *pcm_buf, size_t pcm_buf_len)
{
    sound_pipeline_t pipe;
    const char *uri = s_file_uris[type];
    bool interrupted = false;
    size_t total_pcm_bytes = 0;

    if (audio_service_playback_acquire(1000) != ESP_OK) {
        ESP_LOGW(TAG, "Failed to acquire playback lock");
        return;
    }

    if (pipeline_create(&pipe) != ESP_OK) {
        ESP_LOGE(TAG, "Create pipeline failed");
        audio_service_playback_release();
        return;
    }

    audio_element_set_uri(pipe.spiffs_reader, uri);
    ESP_LOGI(TAG, "Set URI: %s", uri);
    set_active_pipeline(pipe.pipeline);

    s_playing = true;
    s_stop_requested = false;

    if (audio_pipeline_run(pipe.pipeline) != ESP_OK) {
        ESP_LOGE(TAG, "Run pipeline failed");
        s_playing = false;
        clear_active_pipeline(pipe.pipeline);
        pipeline_release(&pipe);
        audio_service_playback_release();
        return;
    }

    ESP_LOGI(TAG, "Playing sound");

    while (!s_stop_requested) {
        int bytes = raw_stream_read(pipe.raw_out, (char *)pcm_buf, (int)pcm_buf_len);
        if (bytes <= 0) {
            break;
        }

        esp_err_t out_ret = audio_service_output_pcm((const int16_t *)pcm_buf, bytes / sizeof(int16_t));
        if (out_ret != ESP_OK) {
            ESP_LOGW(TAG, "PCM output failed: %s", esp_err_to_name(out_ret));
            interrupted = true;
            break;
        }
        total_pcm_bytes += (size_t)bytes;

        /* 新提示音到来时，尽快结束当前短音，下一轮播放最新请求。 */
        if (uxQueueMessagesWaiting(s_play_queue) > 0) {
            interrupted = true;
            break;
        }
    }

    if (s_stop_requested) {
        interrupted = true;
    }

    pipeline_finish(&pipe, interrupted);
    clear_active_pipeline(pipe.pipeline);
    pipeline_release(&pipe);
    s_playing = false;
    audio_service_playback_release();
    ESP_LOGI(TAG, "Playback finished%s, pcm=%u bytes",
             interrupted ? " (interrupted)" : "",
             (unsigned int)total_pcm_bytes);
}

static void playback_task(void *arg)
{
    uint8_t *pcm_buf = (uint8_t *)malloc(PCM_READ_BYTES);
    if (!pcm_buf) {
        ESP_LOGE(TAG, "Failed to alloc PCM buffer");
        vTaskDelete(NULL);
        return;
    }

    while (1) {
        task_sound_type_t type;
        if (xQueueReceive(s_play_queue, &type, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        play_one_sound(type, pcm_buf, PCM_READ_BYTES);
    }
}

/* ── 公共 API ── */

esp_err_t task_sound_init(void)
{
    ESP_LOGI(TAG, "Initializing task sound module");

    s_play_queue = xQueueCreate(1, sizeof(task_sound_type_t));
    if (!s_play_queue) {
        ESP_LOGE(TAG, "Failed to create play queue");
        return ESP_ERR_NO_MEM;
    }

    BaseType_t ret = xTaskCreatePinnedToCore(
        playback_task, "t_sound", PLAYBACK_TASK_STACK,
        NULL, PLAYBACK_TASK_PRIO, &s_play_task, PLAYBACK_TASK_CORE);

    if (ret != pdPASS) {
        ESP_LOGE(TAG, "Failed to create playback task");
        vQueueDelete(s_play_queue);
        s_play_queue = NULL;
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "Task sound module initialized");
    return ESP_OK;
}

esp_err_t task_sound_play(task_sound_type_t type)
{
    if (type < 0 || type > TASK_SOUND_ACTION_CONFIRM) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_play_queue) {
        return ESP_ERR_INVALID_STATE;
    }

    xQueueOverwrite(s_play_queue, &type);
    return ESP_OK;
}

void task_sound_stop(void)
{
    s_stop_requested = true;
    if (s_play_queue) {
        xQueueReset(s_play_queue);
    }
    audio_pipeline_handle_t pipeline = s_active_pipeline;
    if (pipeline) {
        audio_pipeline_stop(pipeline);
    }
}
