#include "esp_log.h"
#include "local_music.h"
#include "audio_element.h"
#include "filter_resample.h"
#include "audio_pipeline.h"
#include "audio_event_iface.h"
#include "raw_stream.h"
#include "mp3_decoder.h"
#include "filter_resample.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "application.h"
#include "audio/audio_service.h"
#include <string.h>
#include <vector>
#include "local_music_define.h"

static const char* TAG = "LOCAL_MUSIC";
static audio_pipeline_handle_t g_pipeline = NULL;
static audio_element_handle_t g_mp3_decoder = NULL;
static audio_element_handle_t g_resample_filter = NULL;
static audio_element_handle_t g_raw_stream = NULL;
static audio_event_iface_handle_t g_evt = NULL;
static TaskHandle_t g_audio_read_task = NULL;
static bool g_audio_task_running = false;
static bool g_music_playing = false;
static bool g_music_finished = false;

static struct marker {
    int pos;
    const uint8_t *start;
    const uint8_t *end;
} file_marker;



void set_mp3_play(const char *name)
{
    auto track = local_music_select_by_name(name);
    if (!track) {
        ESP_LOGE(TAG, "track %s not found", name);
        return;
    }
   file_marker.start=track->start;
   file_marker.end=track->end;
   file_marker.pos=0;
}

int mp3_music_read_cb(audio_element_handle_t el, char *buf, int len, TickType_t wait_time, void *ctx)
{
    int read_size = file_marker.end - file_marker.start - file_marker.pos;
    if (read_size == 0) {
        return AEL_IO_DONE;
    } else if (len < read_size) {
        read_size = len;
    }
    memcpy(buf, file_marker.start + file_marker.pos, read_size);
    file_marker.pos += read_size;
    return read_size;

}

static void audio_read_task(void *pvParameters)
{
    char *audio_buffer = (char*)malloc(1024);
    if (!audio_buffer) {
        vTaskDelete(NULL);
        return;
    }

    g_audio_task_running = true;
    g_music_finished = false;

    auto &audio_service = Application::GetInstance().GetAudioService();

    while (g_audio_task_running && g_raw_stream) {
        int read_len = raw_stream_read(g_raw_stream, audio_buffer, 640);
        if (read_len > 0) {
            int samples = read_len / sizeof(int16_t);
            std::vector<int16_t> pcm_data(samples);
            memcpy(pcm_data.data(), audio_buffer, read_len);
            try {
                audio_service.PushPcmToPlaybackQueue(std::move(pcm_data));
            } catch (const std::exception &) {
            }
        } else if (read_len <= 0) {
            vTaskDelay(pdMS_TO_TICKS(1));
        }

        if (audio_element_get_state(g_mp3_decoder) == AEL_STATE_FINISHED) {
            g_music_finished = true;
            g_audio_task_running = false;
        }
    }

    free(audio_buffer);
    g_audio_read_task = NULL;
    
    local_music_deinit();
    vTaskDelete(NULL);
}

esp_err_t local_music_init(void)
{
    if (g_pipeline) {
        return ESP_OK;
    }

    audio_pipeline_cfg_t pipeline_cfg = DEFAULT_AUDIO_PIPELINE_CONFIG();
    g_pipeline = audio_pipeline_init(&pipeline_cfg);
    if (!g_pipeline) {
        return ESP_FAIL;
    }

    mp3_decoder_cfg_t mp3_cfg = DEFAULT_MP3_DECODER_CONFIG();
    mp3_cfg.task_core = 1;
    mp3_cfg.task_prio = 5;
    g_mp3_decoder = mp3_decoder_init(&mp3_cfg);
    audio_element_set_read_cb(g_mp3_decoder, mp3_music_read_cb, NULL);
    if (!g_mp3_decoder) {
        return ESP_FAIL;
    }

    rsp_filter_cfg_t rsp_cfg = DEFAULT_RESAMPLE_FILTER_CONFIG();
    rsp_cfg.src_rate = 44100;
    rsp_cfg.src_ch = 2;
    rsp_cfg.dest_rate = 24000;
    rsp_cfg.dest_ch = 1;
    rsp_cfg.task_core = 1;
    rsp_cfg.complexity = 2;
    g_resample_filter = rsp_filter_init(&rsp_cfg);
    if (!g_resample_filter) {
        return ESP_FAIL;
    }

    rsp_cfg = DEFAULT_RESAMPLE_FILTER_CONFIG();
    rsp_cfg.src_rate = 8000;  // 源采样率
    rsp_cfg.src_ch = 1;        // 源声道数
    rsp_cfg.dest_rate = 24000; // 目标采样率
    rsp_cfg.dest_ch = 1;       // 目标声道数
    rsp_cfg.task_core = 1;
    rsp_cfg.complexity = 2;
    g_resample_filter = rsp_filter_init(&rsp_cfg);
    if (!g_resample_filter) {
        ESP_LOGE(TAG, "Failed to create resample filter");
    }
    

    raw_stream_cfg_t raw_cfg = RAW_STREAM_CFG_DEFAULT();
    raw_cfg.type = AUDIO_STREAM_READER;
    raw_cfg.out_rb_size = 16 * 1024;
    g_raw_stream = raw_stream_init(&raw_cfg);
    if (!g_raw_stream) {
        return ESP_FAIL;
    }

    audio_pipeline_register(g_pipeline, g_mp3_decoder, "mp3");
    audio_pipeline_register(g_pipeline, g_resample_filter, "resample");
    audio_pipeline_register(g_pipeline, g_raw_stream, "raw");

    const char *link_tag[3];
    link_tag[0] = "mp3";
    link_tag[1] = "resample";
    link_tag[2] = "raw";
    audio_pipeline_link(g_pipeline, &link_tag[0], 3);

    return ESP_OK;
}

esp_err_t local_music_start_play(const char *name)
{   
    if(g_audio_task_running){
        g_audio_task_running = false;
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    set_mp3_play(name);
    if (!g_pipeline) {
        esp_err_t r = local_music_init();
        if (r != ESP_OK) return r;
    }

    if (g_music_playing) {
        audio_pipeline_stop(g_pipeline);
        audio_pipeline_reset_ringbuffer(g_pipeline);
        audio_pipeline_reset_items_state(g_pipeline);
    }

    esp_err_t run_ret = audio_pipeline_run(g_pipeline);
    if (run_ret != ESP_OK) {
        return run_ret;
    }

    if (!g_audio_task_running) {
        g_audio_task_running = true;
        BaseType_t task_ret = xTaskCreatePinnedToCore(
            audio_read_task,
            "local_audio_read_task",
            4096,
            NULL,
            5,
            &g_audio_read_task,
            1
        );
        if (task_ret != pdPASS || !g_audio_read_task) {
            g_audio_task_running = false;
            audio_pipeline_stop(g_pipeline);
            audio_pipeline_wait_for_stop(g_pipeline);
            return ESP_FAIL;
        }
    }

    g_music_playing = true;
    return ESP_OK;
}



esp_err_t local_music_stop(void)
{

    if (g_pipeline) {
        audio_pipeline_stop(g_pipeline);
        audio_pipeline_wait_for_stop(g_pipeline);
        audio_pipeline_reset_ringbuffer(g_pipeline);
    }

    g_music_playing = false;
    return ESP_OK;
}

esp_err_t local_music_deinit(void)
{

    if (g_audio_task_running && g_audio_read_task) {
        g_audio_task_running = false;
    }

    if (g_pipeline) {
        audio_pipeline_stop(g_pipeline);
        audio_pipeline_wait_for_stop(g_pipeline);
        audio_pipeline_terminate(g_pipeline);

        audio_pipeline_unregister(g_pipeline, g_mp3_decoder);
        audio_pipeline_unregister(g_pipeline, g_resample_filter);
        audio_pipeline_unregister(g_pipeline, g_raw_stream);

        audio_element_deinit(g_mp3_decoder);
        audio_element_deinit(g_resample_filter);
        audio_element_deinit(g_raw_stream);

        audio_pipeline_deinit(g_pipeline);
        g_pipeline = NULL;
    }


    g_mp3_decoder = NULL;
    g_resample_filter = NULL;
    g_raw_stream = NULL;
    g_music_playing = false;
    g_audio_task_running = false;
    g_audio_read_task = NULL;

    return ESP_OK;
}
void local_music_wait_for_finish(){
    while(g_audio_task_running)
    {
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}