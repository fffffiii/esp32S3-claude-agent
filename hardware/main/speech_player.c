/*
 * speech_player.c - TTS 语音播放模块
 *
 * ADF 播放链路: http_stream -> mp3_decoder -> rsp_filter -> raw_stream
 * 后台任务从 raw_stream 读取 PCM，写入 audio_service_output_pcm()
 */

#include "speech_player.h"
#include "audio_service.h"
#include "config.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_err.h"
#include "esp_log.h"
#include <stdlib.h>
#include <string.h>

#include "audio_pipeline.h"
#include "audio_element.h"
#include "http_stream.h"
#include "raw_stream.h"
#include "filter_resample.h"
#include "mp3_decoder.h"

#define TAG "SpeechPlayer"

#define PLAYBACK_TASK_STACK  (8 * 1024)
#define PLAYBACK_TASK_PRIO   4
#define PLAYBACK_TASK_CORE   1
#define PCM_READ_BYTES       1024
#define SPEECH_QUEUE_LENGTH  6
#define RAW_READ_TIMEOUT_MS  250
#define RAW_IDLE_TIMEOUT_MS  8000
#define PIPELINE_STOP_TIMEOUT_MS 1500

typedef struct {
    audio_pipeline_handle_t pipeline;
    audio_element_handle_t  http_reader;
    audio_element_handle_t  mp3_dec;
    audio_element_handle_t  resampler;
    audio_element_handle_t  raw_out;
} speech_pipeline_t;

static TaskHandle_t  s_play_task = NULL;
static QueueHandle_t s_play_queue = NULL;
static volatile bool s_playing = false;
static volatile bool s_stop_requested = false;
static volatile audio_pipeline_handle_t s_active_pipeline = NULL;
static volatile audio_element_handle_t s_active_raw_out = NULL;

static void set_active_pipeline(audio_pipeline_handle_t pipeline, audio_element_handle_t raw_out)
{
    s_active_pipeline = pipeline;
    s_active_raw_out = raw_out;
}

static void clear_active_pipeline(audio_pipeline_handle_t pipeline)
{
    if (s_active_pipeline == pipeline) {
        s_active_pipeline = NULL;
        s_active_raw_out = NULL;
    }
}

static bool element_is_terminal(audio_element_handle_t el)
{
    if (!el) {
        return true;
    }

    audio_element_state_t state = audio_element_get_state(el);
    return state == AEL_STATE_FINISHED ||
           state == AEL_STATE_STOPPED ||
           state == AEL_STATE_ERROR;
}

static bool pipeline_upstream_is_terminal(const speech_pipeline_t *p)
{
    return element_is_terminal(p->http_reader) ||
           element_is_terminal(p->mp3_dec) ||
           element_is_terminal(p->resampler);
}

static void pipeline_release(speech_pipeline_t *p)
{
    esp_log_level_t old_element_log = esp_log_level_get("AUDIO_ELEMENT");
    esp_log_level_t old_pipeline_log = esp_log_level_get("AUDIO_PIPELINE");

    esp_log_level_set("AUDIO_ELEMENT", ESP_LOG_ERROR);
    esp_log_level_set("AUDIO_PIPELINE", ESP_LOG_ERROR);

    if (p->pipeline) {
        if (p->http_reader) {
            audio_pipeline_unregister(p->pipeline, p->http_reader);
        }
        if (p->mp3_dec) {
            audio_pipeline_unregister(p->pipeline, p->mp3_dec);
        }
        if (p->resampler) {
            audio_pipeline_unregister(p->pipeline, p->resampler);
        }
        if (p->raw_out) {
            audio_pipeline_unregister(p->pipeline, p->raw_out);
        }
        audio_pipeline_deinit(p->pipeline);
    }
    if (p->http_reader) {
        audio_element_deinit(p->http_reader);
    }
    if (p->mp3_dec) {
        audio_element_deinit(p->mp3_dec);
    }
    if (p->resampler) {
        audio_element_deinit(p->resampler);
    }
    if (p->raw_out) {
        audio_element_deinit(p->raw_out);
    }

    esp_log_level_set("AUDIO_ELEMENT", old_element_log);
    esp_log_level_set("AUDIO_PIPELINE", old_pipeline_log);
    memset(p, 0, sizeof(*p));
}

static esp_err_t pipeline_create(speech_pipeline_t *p, const speech_player_request_t *req)
{
    memset(p, 0, sizeof(*p));

    http_stream_cfg_t http_cfg = HTTP_STREAM_CFG_DEFAULT();
    http_cfg.type = AUDIO_STREAM_READER;
    http_cfg.task_stack = 8 * 1024;
    http_cfg.task_prio = 5;
    http_cfg.out_rb_size = 16 * 1024;
    p->http_reader = http_stream_init(&http_cfg);

    mp3_decoder_cfg_t mp3_cfg = DEFAULT_MP3_DECODER_CONFIG();
    mp3_cfg.task_core = 1;
    mp3_cfg.task_prio = 5;
    p->mp3_dec = mp3_decoder_init(&mp3_cfg);

    rsp_filter_cfg_t rsp_cfg = DEFAULT_RESAMPLE_FILTER_CONFIG();
    rsp_cfg.src_rate   = req->sample_rate ? req->sample_rate : 24000;
    rsp_cfg.src_ch     = 1;
    rsp_cfg.src_bits   = 16;
    rsp_cfg.dest_rate  = AUDIO_OUTPUT_SAMPLE_RATE;
    rsp_cfg.dest_bits  = 16;
    rsp_cfg.dest_ch    = 1;
    rsp_cfg.mode       = RESAMPLE_DECODE_MODE;
    rsp_cfg.complexity = 1;
    rsp_cfg.stack_in_ext = false;
    p->resampler = rsp_filter_init(&rsp_cfg);

    raw_stream_cfg_t raw_cfg = RAW_STREAM_CFG_DEFAULT();
    raw_cfg.type = AUDIO_STREAM_READER;
    p->raw_out = raw_stream_init(&raw_cfg);
    if (p->raw_out) {
        audio_element_set_input_timeout(p->raw_out, pdMS_TO_TICKS(RAW_READ_TIMEOUT_MS));
    }

    if (!p->http_reader || !p->mp3_dec || !p->resampler || !p->raw_out) {
        ESP_LOGE(TAG, "Failed to create pipeline elements");
        pipeline_release(p);
        return ESP_FAIL;
    }

    audio_pipeline_cfg_t pipe_cfg = DEFAULT_AUDIO_PIPELINE_CONFIG();
    p->pipeline = audio_pipeline_init(&pipe_cfg);
    if (!p->pipeline) {
        ESP_LOGE(TAG, "Failed to init pipeline");
        pipeline_release(p);
        return ESP_FAIL;
    }

    audio_pipeline_register(p->pipeline, p->http_reader, "http");
    audio_pipeline_register(p->pipeline, p->mp3_dec, "mp3");
    audio_pipeline_register(p->pipeline, p->resampler, "rsp");
    audio_pipeline_register(p->pipeline, p->raw_out, "raw");

    const char *link_tag[4] = {"http", "mp3", "rsp", "raw"};
    if (audio_pipeline_link(p->pipeline, &link_tag[0], 4) != ESP_OK) {
        ESP_LOGE(TAG, "Failed to link pipeline");
        pipeline_release(p);
        return ESP_FAIL;
    }

    return ESP_OK;
}

static void pipeline_finish(speech_pipeline_t *p, bool interrupted)
{
    (void)interrupted;
    if (p->pipeline) {
        /* 播放自然 EOF 时 ADF 元素会进入 IO_DONE，但 pipeline 未必自动 stop。
         * 这里每次播放结束都显式 stop/wait/terminate，保证播放任务能回到队列。 */
        audio_pipeline_stop(p->pipeline);
        esp_err_t wait_ret = audio_pipeline_wait_for_stop_with_ticks(
            p->pipeline, pdMS_TO_TICKS(PIPELINE_STOP_TIMEOUT_MS));
        if (wait_ret != ESP_OK) {
            ESP_LOGW(TAG, "Wait speech pipeline stop timeout: %s", esp_err_to_name(wait_ret));
        }
        esp_err_t term_ret = audio_pipeline_terminate_with_ticks(
            p->pipeline, pdMS_TO_TICKS(PIPELINE_STOP_TIMEOUT_MS));
        if (term_ret != ESP_OK) {
            ESP_LOGW(TAG, "Terminate speech pipeline timeout: %s", esp_err_to_name(term_ret));
        }
        audio_pipeline_reset_ringbuffer(p->pipeline);
    }
}

static void play_one_speech(const speech_player_request_t *req, uint8_t *pcm_buf, size_t pcm_buf_len)
{
    speech_pipeline_t pipe;
    bool interrupted = false;
    size_t total_pcm_bytes = 0;
    int idle_timeout_ms = 0;

    if (audio_service_playback_acquire(1000) != ESP_OK) {
        ESP_LOGW(TAG, "Failed to acquire playback lock");
        return;
    }

    if (pipeline_create(&pipe, req) != ESP_OK) {
        audio_service_playback_release();
        return;
    }

    audio_element_set_uri(pipe.http_reader, req->audio_url);
    ESP_LOGI(TAG, "Set speech URI: %s (id=%s)", req->audio_url, req->speech_id);
    set_active_pipeline(pipe.pipeline, pipe.raw_out);

    s_playing = true;
    s_stop_requested = false;

    if (audio_pipeline_run(pipe.pipeline) != ESP_OK) {
        ESP_LOGE(TAG, "Run speech pipeline failed");
        s_playing = false;
        clear_active_pipeline(pipe.pipeline);
        pipeline_release(&pipe);
        audio_service_playback_release();
        return;
    }

    ESP_LOGI(TAG, "Playing speech");

    while (!s_stop_requested) {
        int bytes = raw_stream_read(pipe.raw_out, (char *)pcm_buf, (int)pcm_buf_len);
        if (bytes == AEL_IO_TIMEOUT) {
            if (pipeline_upstream_is_terminal(&pipe)) {
                ESP_LOGW(TAG, "Speech upstream ended without PCM, id=%s http=%d mp3=%d rsp=%d",
                         req->speech_id,
                         audio_element_get_state(pipe.http_reader),
                         audio_element_get_state(pipe.mp3_dec),
                         audio_element_get_state(pipe.resampler));
                interrupted = true;
                break;
            }

            idle_timeout_ms += RAW_READ_TIMEOUT_MS;
            if (idle_timeout_ms >= RAW_IDLE_TIMEOUT_MS) {
                ESP_LOGW(TAG, "Speech raw stream idle timeout, id=%s pcm=%u bytes",
                         req->speech_id, (unsigned int)total_pcm_bytes);
                interrupted = true;
                break;
            }
            continue;
        }
        if (bytes <= 0) {
            ESP_LOGI(TAG, "Speech raw stream done, bytes=%d", bytes);
            break;
        }
        idle_timeout_ms = 0;

        esp_err_t out_ret = audio_service_output_pcm((const int16_t *)pcm_buf, bytes / (int)sizeof(int16_t));
        if (out_ret != ESP_OK) {
            ESP_LOGW(TAG, "PCM output failed: %s", esp_err_to_name(out_ret));
            interrupted = true;
            break;
        }
        total_pcm_bytes += (size_t)bytes;

        /* 等当前语音自然播完后再取下一条，避免连续 TTS 互相打断。 */
    }

    if (s_stop_requested) {
        interrupted = true;
    }

    pipeline_finish(&pipe, interrupted);
    clear_active_pipeline(pipe.pipeline);
    pipeline_release(&pipe);
    s_playing = false;
    audio_service_playback_release();
    ESP_LOGI(TAG, "Speech finished%s, pcm=%u bytes",
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
        speech_player_request_t req;
        if (xQueueReceive(s_play_queue, &req, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        play_one_speech(&req, pcm_buf, PCM_READ_BYTES);
    }
}

esp_err_t speech_player_init(void)
{
    ESP_LOGI(TAG, "Initializing speech player");

    if (s_play_queue) {
        return ESP_OK;
    }

    s_play_queue = xQueueCreate(SPEECH_QUEUE_LENGTH, sizeof(speech_player_request_t));
    if (!s_play_queue) {
        ESP_LOGE(TAG, "Failed to create speech queue");
        return ESP_ERR_NO_MEM;
    }

    BaseType_t ret = xTaskCreatePinnedToCore(
        playback_task, "t_speech", PLAYBACK_TASK_STACK,
        NULL, PLAYBACK_TASK_PRIO, &s_play_task, PLAYBACK_TASK_CORE);

    if (ret != pdPASS) {
        ESP_LOGE(TAG, "Failed to create speech playback task");
        vQueueDelete(s_play_queue);
        s_play_queue = NULL;
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "Speech player initialized");
    return ESP_OK;
}

esp_err_t speech_player_play(const speech_player_request_t *request)
{
    if (!request || request->audio_url[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_play_queue) {
        return ESP_ERR_INVALID_STATE;
    }

    s_stop_requested = false;
    if (xQueueSend(s_play_queue, request, 0) != pdTRUE) {
        speech_player_request_t dropped;
        (void)xQueueReceive(s_play_queue, &dropped, 0);
        (void)xQueueSend(s_play_queue, request, 0);
        ESP_LOGW(TAG, "Speech queue full, dropped oldest request");
    }
    return ESP_OK;
}

void speech_player_stop(void)
{
    s_stop_requested = true;
    if (s_play_queue) {
        xQueueReset(s_play_queue);
    }
    audio_pipeline_handle_t pipeline = s_active_pipeline;
    audio_element_handle_t raw_out = s_active_raw_out;
    if (raw_out) {
        audio_element_abort_input_ringbuf(raw_out);
    }
    if (pipeline) {
        audio_pipeline_stop(pipeline);
    }
}
