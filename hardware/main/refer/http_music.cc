#include "http_music.h"
#include "esp_log.h"
#include "esp_http_client.h"
#include "cJSON.h"
#include <string.h>
#include <stdlib.h>
#include <time.h>
#include "board.h"
#include "display/display.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "audio_element.h"
#include "audio_pipeline.h"
#include "audio_event_iface.h"
#include "audio_common.h"
#include "http_stream.h"
#include "raw_stream.h"
#include "mp3_decoder.h"
#include "filter_resample.h"
#include "board.h"
#include "audio/audio_service.h"
#include "application.h"
#include "device_state.h"




static const char* TAG = "HTTP_MUSIC";
#define ALARM_MUSIC_URL "https://6368-chainetic-aiot-9g385us8bffe8468-1368780781.tcb.qcloud.la/musics/alarm_music.mp3?sign=3d94a04131382bce6e2b7636135b655b&t=1759978762"
#define MAX_HTTP_RECV_BUFFER 8192
static char http_response_buffer[MAX_HTTP_RECV_BUFFER];
static int http_response_len = 0;

static std::string music_message = "";
bool pipeline_stoping = false;
// 音频管道相关全局变量
static audio_pipeline_handle_t g_pipeline = NULL;
static audio_element_handle_t g_http_stream = NULL;
static audio_element_handle_t g_mp3_decoder = NULL;
static audio_element_handle_t g_resample_filter = NULL;
static audio_element_handle_t g_raw_stream = NULL;
static audio_event_iface_handle_t g_evt = NULL;
static bool g_music_playing = false;
static bool music_end = false;
// 音频读取任务相关
static TaskHandle_t g_audio_read_task = NULL;
static bool g_audio_task_running = false;
static bool music_start_play = false;
// 音乐URL存储
static char g_music_url[512] = {0};
static bool is_alarm_music = false;
// URL编码函数
static char* url_encode(const char* str) {
    if (!str) return NULL;
    size_t len = strlen(str);
    // 最坏情况下每个字节都需要编码为%XX，所以分配3倍空间
    char* encoded = (char*)malloc(len * 3 + 1);
    if (!encoded) return NULL;
    
    char* p = encoded;
    for (size_t i = 0; i < len; i++) {
        unsigned char c = (unsigned char)str[i];
        // 保留字母、数字和一些安全字符
        if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || 
            (c >= '0' && c <= '9') || c == '-' || c == '_' || 
            c == '.' || c == '~') {
            *p++ = c;
        } else if (c == ' ') {
            *p++ = '+';
        } else {
            // 对其他字符进行百分号编码
            sprintf(p, "%%%02X", c);
            p += 3;
        }
    }
    *p = '\0';
    return encoded;
}

// 音频读取任务
static void audio_read_task(void *pvParameters)
{
    char *audio_buffer = (char*)malloc(1024); // 音频缓冲区
    if (!audio_buffer) {
        ESP_LOGE(TAG, "Failed to allocate audio buffer");
        vTaskDelete(NULL);
        return;
    }
    
    g_audio_task_running = true;
    music_end = false;
    music_start_play = false;
    // 获取AudioService实例
    auto& audio_service = Application::GetInstance().GetAudioService();
    auto& app = Application::GetInstance();
    app.PlayMusicToggleState();

    while (g_audio_task_running && g_raw_stream) {
        auto device_state = Application::GetInstance().GetDeviceState();

        if (device_state == kDeviceStateSpeaking||device_state == kDeviceStateListening) {
                // 设备开始说话，暂停管道
                if(pipeline_stoping == false){
                ESP_LOGI(TAG, "Device speaking, pausing audio pipeline");
                audio_pipeline_stop(g_pipeline);
                    pipeline_stoping = true;
                }
                if(music_start_play){
                http_music_deinit();
                break;
                }
            }
                else{
                // 设备停止说话，恢复管道
                if(pipeline_stoping == true&&music_end == false){
                music_start_play = true;
                set_music_display_status(false);
                ESP_LOGI(TAG, "Device stopped speaking, resuming audio pipeline");
                audio_pipeline_resume(g_pipeline);
                pipeline_stoping = false;
                }
            }
    
        
        // 如果设备正在说话，跳过PCM数据处理
        if (device_state == kDeviceStateSpeaking) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        
        // 使用raw_stream_read获取音频数据
        int read_len = raw_stream_read(g_raw_stream, audio_buffer, 640);
        music_start_play = true;
        if (read_len > 0) {
            
            // 将音频数据转换为int16_t格式
            int samples = read_len / sizeof(int16_t);
            std::vector<int16_t> pcm_data(samples);
            memcpy(pcm_data.data(), audio_buffer, read_len);
            
            // 直接发送PCM数据到播放队列
            try {
                audio_service.PushPcmToPlaybackQueue(std::move(pcm_data));
            } catch (const std::exception& e) {
                ESP_LOGW(TAG, "Failed to push PCM data to playback queue: %s", e.what());
            }
        }
        else if (read_len <= 0) {
            // 没有数据或出错，短暂延时
            vTaskDelay(pdMS_TO_TICKS(1));
        }
    if(audio_element_get_state(g_http_stream) == AEL_STATE_FINISHED){
        music_end = true;
        auto& board = Board::GetInstance();
        auto display = board.GetDisplay();
        set_music_display_status(true);
        pipeline_stoping = false;
        http_music_deinit();
        // audio_pipeline_stop(g_pipeline);
        }
    }  

    free(audio_buffer);
    g_audio_read_task = NULL;
    ESP_LOGI(TAG, "Audio read task stopped");
    vTaskDelete(NULL);
}

// HTTP事件处理函数
static esp_err_t music_http_event_handler(esp_http_client_event_t *evt)
{
    switch(evt->event_id) {
        case HTTP_EVENT_ERROR:
            ESP_LOGD(TAG, "HTTP_EVENT_ERROR");
            break;
        case HTTP_EVENT_ON_CONNECTED:
            ESP_LOGD(TAG, "HTTP_EVENT_ON_CONNECTED");
            break;
        case HTTP_EVENT_HEADER_SENT:
            ESP_LOGD(TAG, "HTTP_EVENT_HEADER_SENT");
            break;
        case HTTP_EVENT_ON_HEADER:
            ESP_LOGD(TAG, "HTTP_EVENT_ON_HEADER, key=%s, value=%s", evt->header_key, evt->header_value);
            break;
        case HTTP_EVENT_ON_DATA:
            ESP_LOGD(TAG, "HTTP_EVENT_ON_DATA, len=%d", evt->data_len);
            if (!esp_http_client_is_chunked_response(evt->client)) {
                // 非分块传输，直接复制数据
                if (http_response_len + evt->data_len < MAX_HTTP_RECV_BUFFER) {
                    memcpy(http_response_buffer + http_response_len, evt->data, evt->data_len);
                    http_response_len += evt->data_len;
                }
            } else {
                // 分块传输，逐块复制数据
                if (http_response_len + evt->data_len < MAX_HTTP_RECV_BUFFER) {
                    memcpy(http_response_buffer + http_response_len, evt->data, evt->data_len);
                    http_response_len += evt->data_len;
                }
            }
            break;
        case HTTP_EVENT_ON_FINISH:
            ESP_LOGD(TAG, "HTTP_EVENT_ON_FINISH");
            break;
        case HTTP_EVENT_DISCONNECTED:
            ESP_LOGD(TAG, "HTTP_EVENT_DISCONNECTED");
            break;
        default:
            break;
    }
    return ESP_OK;
}

esp_err_t http_music_init(void) {
    // 声明所有变量在函数开头，避免 goto 跨越初始化的问题
    audio_pipeline_cfg_t pipeline_cfg;
    http_stream_cfg_t http_cfg;
    mp3_decoder_cfg_t mp3_cfg;
    rsp_filter_cfg_t rsp_cfg;
    raw_stream_cfg_t raw_cfg;
    const char *link_tag[4];
    audio_event_iface_cfg_t evt_cfg;
    
    if (g_pipeline) {
        ESP_LOGW(TAG, "HTTP music player already initialized");
        return ESP_OK;
    }
    
    ESP_LOGI(TAG, "[1.0] Create audio pipeline for HTTP playback");
    pipeline_cfg = DEFAULT_AUDIO_PIPELINE_CONFIG();
    g_pipeline = audio_pipeline_init(&pipeline_cfg);
    if (!g_pipeline) {
        ESP_LOGE(TAG, "Failed to create pipeline");
        return ESP_FAIL;
    }
    
    ESP_LOGI(TAG, "[1.1] Create HTTP stream");
    http_cfg = HTTP_STREAM_CFG_DEFAULT();
    http_cfg.task_core = 1;
    http_cfg.task_prio = 5;
    http_cfg.task_stack = 10*1024;
    http_cfg.out_rb_size  = 30*1024;
    g_http_stream = http_stream_init(&http_cfg);
    if (!g_http_stream) {
        ESP_LOGE(TAG, "Failed to create HTTP stream");
        goto err_cleanup;
    }
    
    ESP_LOGI(TAG, "[1.2] Create MP3 decoder");
    mp3_cfg = DEFAULT_MP3_DECODER_CONFIG();
    mp3_cfg.task_core = 1;
    mp3_cfg.task_prio = 5;
    g_mp3_decoder = mp3_decoder_init(&mp3_cfg);
    if (!g_mp3_decoder) {
        ESP_LOGE(TAG, "Failed to create MP3 decoder");
        goto err_cleanup;
    }
    
    ESP_LOGI(TAG, "[1.4] Create resample filter");
    rsp_cfg = DEFAULT_RESAMPLE_FILTER_CONFIG();
    rsp_cfg.src_rate = 44100;  // 源采样率
    rsp_cfg.src_ch = 2;        // 源声道数
    rsp_cfg.dest_rate = 24000; // 目标采样率
    rsp_cfg.dest_ch = 1;       // 目标声道数
    rsp_cfg.task_core = 1;
    rsp_cfg.complexity = 2;
    g_resample_filter = rsp_filter_init(&rsp_cfg);
    if (!g_resample_filter) {
        ESP_LOGE(TAG, "Failed to create resample filter");
        goto err_cleanup;
    }
    
    ESP_LOGI(TAG, "[1.5] Create RAW stream");
    raw_cfg = RAW_STREAM_CFG_DEFAULT();
    raw_cfg.type = AUDIO_STREAM_READER;
    raw_cfg.out_rb_size = 16 * 1024;
    g_raw_stream = raw_stream_init(&raw_cfg);
    if (!g_raw_stream) {
        ESP_LOGE(TAG, "Failed to create RAW stream");
        goto err_cleanup;
    }
    
    ESP_LOGI(TAG, "[1.6] Register elements to pipeline");
    audio_pipeline_register(g_pipeline, g_http_stream, "http");
    audio_pipeline_register(g_pipeline, g_mp3_decoder, "mp3");
    audio_pipeline_register(g_pipeline, g_resample_filter, "resample");
    audio_pipeline_register(g_pipeline, g_raw_stream, "raw");
    
    // 链接MP3管道
    ESP_LOGI(TAG, "[1.7] Link elements: http->mp3->resample->raw");
    link_tag[0] = "http";
    link_tag[1] = "mp3";
    link_tag[2] = "resample";
    link_tag[3] = "raw";
    audio_pipeline_link(g_pipeline, &link_tag[0], 4);
    
    ESP_LOGI(TAG, "[1.8] Set up event listener");
    evt_cfg = AUDIO_EVENT_IFACE_DEFAULT_CFG();
    g_evt = audio_event_iface_init(&evt_cfg);
    if (!g_evt) {
        ESP_LOGE(TAG, "Failed to create event interface");
        goto err_cleanup;
    }
    
    audio_pipeline_set_listener(g_pipeline, g_evt);
    
    ESP_LOGI(TAG, "HTTP music player initialized successfully");
    return ESP_OK;
    
err_cleanup:
    // 这里应该调用清理函数，但为了简化先返回错误
    return ESP_FAIL;
}
esp_err_t http_music_stop(void) {
    ESP_LOGI(TAG, "Stopping music playback");
    
    if (!g_music_playing) {
        ESP_LOGW(TAG, "Music is not playing");
        return ESP_OK;
    }
    
    // 停止音频读取任务
    if (g_audio_task_running && g_audio_read_task) {
        g_audio_task_running = false;
        vTaskDelete(g_audio_read_task);
        g_audio_read_task = NULL;
        ESP_LOGI(TAG, "Audio read task stopped");
    }
    
    // 停止音频管道
    if (g_pipeline) {
        audio_pipeline_stop(g_pipeline);
        audio_pipeline_wait_for_stop(g_pipeline);
        audio_pipeline_reset_ringbuffer(g_pipeline);
        ESP_LOGI(TAG, "Audio pipeline stopped");
    }
    
    g_music_playing = false;
    ESP_LOGI(TAG, "Music playback stopped successfully");
    
    return ESP_OK;
}

esp_err_t fetch_music_info_and_lyrics(const char* song_name) {
    if (!song_name) {
        ESP_LOGE(TAG, "Song name is NULL");
        return ESP_ERR_INVALID_ARG;
    }
    
    // 清理歌曲名称（移除句号等标点符号）
    char cleaned_name[256];
    strncpy(cleaned_name, song_name, sizeof(cleaned_name) - 1);
    cleaned_name[sizeof(cleaned_name) - 1] = '\0';

    
    if (strlen(cleaned_name) == 0) {
        ESP_LOGE(TAG, "Song name is empty after sanitization");
        return ESP_ERR_INVALID_ARG;
    }
    
    ESP_LOGI(TAG, "Cleaned song name: %s", cleaned_name);
    
    // URL编码歌曲名称
    char* encoded_name = url_encode(cleaned_name);
    if (!encoded_name) {
        ESP_LOGE(TAG, "Failed to encode song name");
        return ESP_ERR_NO_MEM;
    }
    
    ESP_LOGI(TAG, "Encoded song name: %s", encoded_name);
    
    // 动态分配URL缓冲区
    size_t url_size = 256 + strlen(encoded_name);
    char* url = (char*)malloc(url_size);
    if (!url) {
        ESP_LOGE(TAG, "Failed to allocate memory for URL");
        free(encoded_name);
        return ESP_ERR_NO_MEM;
    }
    
    // 构建API URL
    snprintf(url, url_size, 
        "https://api.yaohud.cn/api/music/wyvip?key=B04jL9RQMU0wlcPz4fX&g=1&msg=%s&n=1&level=standard", 
        encoded_name);
    
    ESP_LOGI(TAG, "Fetching music: %s", url);
    
    // 清空响应缓冲区
    memset(http_response_buffer, 0, sizeof(http_response_buffer));
    http_response_len = 0;
    
    // 配置HTTP客户端
    esp_http_client_config_t config = {};
    config.url = url;
    config.event_handler = music_http_event_handler;
    config.timeout_ms = 10000;
    
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) {
        ESP_LOGE(TAG, "Failed to initialize HTTP client");
        free(url);
        free(encoded_name);
        return ESP_FAIL;
    }
    
    esp_err_t result = ESP_OK;
    
    // 执行HTTP GET请求
    esp_err_t err = esp_http_client_perform(client);
    if (err == ESP_OK) {
        int status_code = esp_http_client_get_status_code(client);
        int content_length = esp_http_client_get_content_length(client);
        ESP_LOGI(TAG, "HTTP GET Status = %d, content_length = %d, response_len = %d", 
                status_code, content_length, http_response_len);
        
        if (status_code == 200) {
            // 确保响应缓冲区以null结尾
            if (http_response_len > 0 && http_response_len < MAX_HTTP_RECV_BUFFER) {
                http_response_buffer[http_response_len] = '\0';
            }
            
            ESP_LOGI(TAG, "Response data: %s", http_response_buffer);
            
            // 解析JSON响应
            cJSON *json = cJSON_Parse(http_response_buffer);
            if (json != NULL) {
                ESP_LOGI(TAG, "JSON parsed successfully");
                cJSON *code = cJSON_GetObjectItem(json, "code");
                if (cJSON_IsNumber(code) && code->valueint == 200) {
                    ESP_LOGI(TAG, "API code is 200, proceeding to parse data");
                    cJSON *data = cJSON_GetObjectItem(json, "data");
                    if (cJSON_IsObject(data)) {
                        ESP_LOGI(TAG, "Data object found");
                        
                        // 获取歌曲信息
                        cJSON *name = cJSON_GetObjectItem(data, "name");
                        cJSON *songname = cJSON_GetObjectItem(data, "songname");
                        
                        if (cJSON_IsString(name) && cJSON_IsString(songname)) {
                            printf("=== 歌曲信息 ===\n");
                            printf("歌手: %s\n", name->valuestring);
                            printf("歌曲: %s\n", songname->valuestring);
                            music_message = "正在播放：" + std::string(name->valuestring) + " - " + std::string(songname->valuestring);

                            ESP_LOGI(TAG, "Song info: %s - %s", name->valuestring, songname->valuestring);
                        }
                        
                        // 获取音乐链接
                        char *music_url = NULL;
                        
                        // 优先获取vipmusic对象中的url
                        cJSON *vipmusic = cJSON_GetObjectItem(data, "vipmusic");
                        if (cJSON_IsObject(vipmusic)) {
                            ESP_LOGI(TAG, "VIP music object found");
                            cJSON *vip_url = cJSON_GetObjectItem(vipmusic, "url");
                            if (cJSON_IsString(vip_url) && vip_url->valuestring != NULL && strlen(vip_url->valuestring) > 0) {
                                music_url = vip_url->valuestring;
                                ESP_LOGI(TAG, "Found VIP music URL: %s", music_url);
                            }
                        }
                        
                        // 如果vipmusic中没有找到有效URL，则使用其他字段作为备选
                        if (music_url == NULL) {
                            ESP_LOGI(TAG, "VIP music URL not found, trying fallback options");
                            // 尝试musicurl字段
                            cJSON *musicurl = cJSON_GetObjectItem(data, "musicurl");
                            if (cJSON_IsString(musicurl) && musicurl->valuestring != NULL && strlen(musicurl->valuestring) > 0) {
                                music_url = musicurl->valuestring;
                                ESP_LOGI(TAG, "Found fallback music URL from musicurl: %s", music_url);
                            } else {
                                // 最后尝试url字段
                                cJSON *url_field = cJSON_GetObjectItem(data, "url");
                                if (cJSON_IsString(url_field) && url_field->valuestring != NULL && strlen(url_field->valuestring) > 0) {
                                    music_url = url_field->valuestring;
                                    ESP_LOGI(TAG, "Found basic music URL: %s", music_url);
                                }
                            }
                        }
                        
                        if (music_url != NULL) {
                            // 将音乐URL保存到全局变量中
                            strncpy(g_music_url, music_url, sizeof(g_music_url) - 1);
                            g_music_url[sizeof(g_music_url) - 1] = '\0';
                            ESP_LOGI(TAG, "Music URL: %s", music_url);
                        } else {
                            // 清空全局URL变量
                            g_music_url[0] = '\0';
                            ESP_LOGE(TAG, "No valid music URL found in response");
                        }
                        
                        // 获取并打印歌词
                        cJSON *music = cJSON_GetObjectItem(data, "music");
                        if (cJSON_IsObject(music)) {
                            cJSON *lyrics = cJSON_GetObjectItem(music, "lrc");
                            if (cJSON_IsString(lyrics) && lyrics->valuestring != NULL) {
                                printf("=== 歌词 ===\n");
                                printf("%s\n", lyrics->valuestring);
                                ESP_LOGI(TAG, "Lyrics found and printed");
                               // free(lyrics->valuestring);
                            } else {
                                ESP_LOGI(TAG, "No lyrics found in music object");
                            }
                        } else {
                            ESP_LOGI(TAG, "No music object found in response");
                        }
                        
                        printf("================\n");
                        
                    } else {
                        ESP_LOGE(TAG, "Data field is not an object or not found");
                        result = ESP_FAIL;
                    }
                } else {
                    ESP_LOGE(TAG, "API returned error code: %d", code ? code->valueint : -1);
                    result = ESP_FAIL;
                }
                cJSON_Delete(json);
            } else {
                ESP_LOGE(TAG, "Failed to parse JSON response: %s", http_response_buffer);
                result = ESP_FAIL;
            }
        } else {
            ESP_LOGE(TAG, "HTTP request failed with status: %d", status_code);
            result = ESP_FAIL;
        }
    } else {
        ESP_LOGE(TAG, "HTTP GET request failed: %s", esp_err_to_name(err));
        result = ESP_FAIL;
    }
    
    esp_http_client_cleanup(client);
    free(url);
    free(encoded_name);
    
    return result;
}

// 音频事件处理函数
void http_music_process_events(void) {
    if (!g_evt) {
        return;
    }
    
    audio_event_iface_msg_t msg;
    esp_err_t ret = audio_event_iface_listen(g_evt, &msg, 100 / portTICK_PERIOD_MS);
    
    if (ret == ESP_OK) {
        if (msg.source_type == AUDIO_ELEMENT_TYPE_ELEMENT) {
            if (msg.cmd == AEL_MSG_CMD_REPORT_STATUS) {
                audio_element_state_t el_state = audio_element_get_state((audio_element_handle_t)msg.source);
                if (el_state == AEL_STATE_FINISHED) {
                    ESP_LOGI(TAG, "Audio playback finished");
                    g_music_playing = false;
                    
                    // 停止音频读取任务
                    if (g_audio_task_running && g_audio_read_task) {
                        g_audio_task_running = false;
                        vTaskDelete(g_audio_read_task);
                        g_audio_read_task = NULL;
                    }
                    
                    // 停止管道
                    audio_pipeline_stop(g_pipeline);
                    audio_pipeline_wait_for_stop(g_pipeline);
                    audio_pipeline_reset_ringbuffer(g_pipeline);
                }
            }
        }
    }
}

// 启动音乐播放 - 集成音频管道检查、音乐地址获取和播放功能
esp_err_t start_play_music(const char* music_name) {
    // 1. 参数验证
    if (!music_name) {
        ESP_LOGE(TAG, "Music name is NULL");
        return ESP_ERR_INVALID_ARG;
    }
    
    ESP_LOGI(TAG, "Starting music playback for: %s", music_name);
    music_start_play = false;
    // 2. 检查音频管道是否已初始化
    if (!g_pipeline) {
        ESP_LOGW(TAG, "Audio pipeline not initialized, initializing now...");
        esp_err_t init_ret = http_music_init();
        if (init_ret != ESP_OK) {
            ESP_LOGE(TAG, "Failed to initialize audio pipeline: %s", esp_err_to_name(init_ret));
            return init_ret;
        }
        ESP_LOGI(TAG, "Audio pipeline initialized successfully");
    }
    
    // 3. 停止当前播放（如果有）
    if (g_music_playing) {
        ESP_LOGI(TAG, "Stopping current playback...");
        audio_pipeline_stop(g_pipeline);
        audio_pipeline_reset_ringbuffer(g_pipeline);
        audio_pipeline_reset_items_state(g_pipeline);
    }
    
    // 4. 获取音乐信息和URL
    ESP_LOGI(TAG, "Fetching music info and URL for: %s", music_name);
    esp_err_t fetch_ret = fetch_music_info_and_lyrics(music_name);
    if (fetch_ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to fetch music info and lyrics: %s", esp_err_to_name(fetch_ret));
        return fetch_ret;
    }
    
    // 5. 验证音乐URL
    if (strlen(g_music_url) == 0) {
        ESP_LOGE(TAG, "No music URL found for: %s", music_name);
        return ESP_FAIL;
    }
    
    ESP_LOGI(TAG, "Music URL obtained: %s", g_music_url);
    
    // 6. 配置和启动音频播放
    ESP_LOGI(TAG, "Configuring audio pipeline for playback...");
    audio_element_reset_state(g_http_stream);
    audio_element_reset_state(g_mp3_decoder);
    audio_element_reset_state(g_resample_filter);
    // 设置HTTP流的URI
    esp_err_t uri_ret = audio_element_set_uri(g_http_stream, g_music_url);
    if (uri_ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to set audio URI: %s", esp_err_to_name(uri_ret));
        return uri_ret;
    }
    
    is_alarm_music = false;

    // 启动音频管道
    if(g_audio_task_running == false){
    esp_err_t run_ret = audio_pipeline_run(g_pipeline);
    if (run_ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to run audio pipeline: %s", esp_err_to_name(run_ret));
        return run_ret;
    }
}
    
    // 7. 等待管道启动
    // ESP_LOGI(TAG, "Waiting for audio pipeline to start...");
    // audio_element_state_t el_state = audio_element_get_state(g_http_stream);
    // int wait_count = 0;
    // const int max_wait_count = 50; // 最多等待5秒
    
    // while (el_state != AEL_STATE_RUNNING && el_state != AEL_STATE_PAUSED && wait_count < max_wait_count) {
    //     vTaskDelay(100 / portTICK_PERIOD_MS);
    //     el_state = audio_element_get_state(g_http_stream);
    //     wait_count++;
    // }
    
    // if (wait_count >= max_wait_count) {
    //     ESP_LOGE(TAG, "Timeout waiting for audio pipeline to start");
    //     audio_pipeline_stop(g_pipeline);
    //     audio_pipeline_wait_for_stop(g_pipeline);
    //     return ESP_FAIL;
    // }
    
    // 8. 创建音频读取任务
    if (!g_audio_task_running) {
        ESP_LOGI(TAG, "Creating audio read task...");
        g_audio_task_running = true;
        BaseType_t task_ret = xTaskCreatePinnedToCore(
            audio_read_task,
            "audio_read_task",
            4096,
            NULL,
            5,
            &g_audio_read_task,
            1
        );
        
        if (task_ret != pdPASS || !g_audio_read_task) {
            ESP_LOGE(TAG, "Failed to create audio read task");
            g_audio_task_running = false;
            audio_pipeline_stop(g_pipeline);
            audio_pipeline_wait_for_stop(g_pipeline);
            return ESP_FAIL;
        }
        ESP_LOGI(TAG, "Audio read task created successfully");
    }
    
    // 9. 设置播放状态
    g_music_playing = true;
    ESP_LOGI(TAG, "Music playback started successfully for: %s", music_name);
    
    return ESP_OK;
}
esp_err_t alarm_start_play_music() {

    ESP_LOGI(TAG, "Starting alarm music playback...");

   if (!g_pipeline) {
        ESP_LOGW(TAG, "Audio pipeline not initialized, initializing now...");
        esp_err_t init_ret = http_music_init();
        if (init_ret != ESP_OK) {
            ESP_LOGE(TAG, "Failed to initialize audio pipeline: %s", esp_err_to_name(init_ret));
            return init_ret;
        }
        ESP_LOGI(TAG, "Audio pipeline initialized successfully");
    }
    
    if (g_music_playing) {
        ESP_LOGI(TAG, "Stopping current playback...");
        audio_pipeline_stop(g_pipeline);
        audio_pipeline_reset_ringbuffer(g_pipeline);
        audio_pipeline_reset_items_state(g_pipeline);
    }
    
    audio_element_reset_state(g_http_stream);
    audio_element_reset_state(g_mp3_decoder);
    audio_element_reset_state(g_resample_filter);
    // 设置HTTP流的URI
    esp_err_t uri_ret = audio_element_set_uri(g_http_stream, ALARM_MUSIC_URL);
    if (uri_ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to set alarm music URI: %s", esp_err_to_name(uri_ret));
        return uri_ret;
    }

    is_alarm_music = true;
    // auto device_state = Application::GetInstance().GetDeviceState();
    // if(device_state == kDeviceStateIdle){
    //     ESP_LOGI(TAG, "device_state is idle, start play music");
    //     music_start_play = true ;
    // }
    // else{
    //     music_start_play = false;
    // }
    if(g_audio_task_running == false){
    esp_err_t run_ret = audio_pipeline_run(g_pipeline);
    if (run_ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to run audio pipeline: %s", esp_err_to_name(run_ret));
        return run_ret;
        }
    }
    // 8. 创建音频读取任务
    if (!g_audio_task_running) {
        ESP_LOGI(TAG, "Creating audio read task...");
        g_audio_task_running = true;
        BaseType_t task_ret = xTaskCreatePinnedToCore(
            audio_read_task,
            "audio_read_task",
            4096,
            NULL,
            5,
            &g_audio_read_task,
            1
        );
        
        if (task_ret != pdPASS || !g_audio_read_task) {
            ESP_LOGE(TAG, "Failed to create audio read task");
            g_audio_task_running = false;
            audio_pipeline_stop(g_pipeline);
            audio_pipeline_wait_for_stop(g_pipeline);
            return ESP_FAIL;
        }
        ESP_LOGI(TAG, "Audio read task created successfully");
    }
    
    // 9. 设置播放状态
    g_music_playing = true;
    ESP_LOGI(TAG, "Alarm music playback started successfully");
    return ESP_OK;

}
// 清理函数
esp_err_t http_music_deinit(void) {
    ESP_LOGI(TAG, "Deinitializing HTTP music player");
    
    // 停止播放
    if (g_music_playing) {
        http_music_stop();
    }
    
    // 停止音频读取任务
    if (g_audio_task_running && g_audio_read_task) {
        g_audio_task_running = false;
        // vTaskDelete(g_audio_read_task);
        // g_audio_read_task = NULL;
    }
    
    // 清理音频管道
    if (g_pipeline) {
        audio_pipeline_stop(g_pipeline);
        audio_pipeline_wait_for_stop(g_pipeline);
        audio_pipeline_terminate(g_pipeline);
        
        // 注销元素
        audio_pipeline_unregister(g_pipeline, g_http_stream);
        audio_pipeline_unregister(g_pipeline, g_mp3_decoder);
        audio_pipeline_unregister(g_pipeline, g_resample_filter);
        audio_pipeline_unregister(g_pipeline, g_raw_stream);
        
        // 清理元素
        audio_element_deinit(g_http_stream);
        audio_element_deinit(g_mp3_decoder);
        audio_element_deinit(g_resample_filter);
        audio_element_deinit(g_raw_stream);
        
        // 清理管道
        audio_pipeline_deinit(g_pipeline);
        g_pipeline = NULL;
    }
    
    // 清理事件接口
    if (g_evt) {
        audio_event_iface_destroy(g_evt);
        g_evt = NULL;
    }
    
    // 重置全局变量
    g_http_stream = NULL;
    g_mp3_decoder = NULL;
    g_resample_filter = NULL;
    g_raw_stream = NULL;
    g_music_playing = false;
    g_audio_task_running = false;
    g_audio_read_task = NULL;
    
    ESP_LOGI(TAG, "HTTP music player deinitialized");
    return ESP_OK;
}

void set_music_display_status(bool is_end){
    if(!is_end){
        if(is_alarm_music == false){
        auto& board = Board::GetInstance();
        auto display = board.GetDisplay();
        display->SetStatus("音乐模式");
        display->SetEmotion("happy");
        display->SetChatMessage("system", music_message.c_str());
    }
    else{
        auto& board = Board::GetInstance();
        auto display = board.GetDisplay();
        display->SetStatus("闹钟模式");
        display->SetEmotion("laughing");
        display->SetChatMessage("system","闹钟响铃中...");
        // 获取当前时间并格式化消息
    }
    }
    else{
        if(is_alarm_music == false){
        auto& board = Board::GetInstance();
        auto display = board.GetDisplay();
        display->SetStatus("音乐模式");
        display->SetEmotion("sleepy");
        display->SetChatMessage("system", "音乐播放结束");
    }
    else{
        auto& board = Board::GetInstance();
        auto display = board.GetDisplay();
        display->SetStatus("闹钟模式");
        display->SetEmotion("sleepy");
        display->SetChatMessage("system", "闹钟播放结束");
    }
    }
}