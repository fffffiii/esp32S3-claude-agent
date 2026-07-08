#include "wake_engine.h"

#include <cstring>
#include <string>
#include <esp_log.h>
#include <esp_mn_iface.h>
#include <esp_mn_models.h>
#include <esp_mn_speech_commands.h>
#include <model_path.h>

#define TAG "WakeEngine"
#define MAX_COMMANDS 16

struct WakeCommand {
    std::string command;
    std::string text;
};

struct WakeEngineImpl {
    esp_mn_iface_t *multinet = nullptr;
    model_iface_data_t *model_data = nullptr;
    srmodel_list_t *models = nullptr;
    char *mn_name = nullptr;
    std::string language;
    int duration_ms;
    float threshold;
    bool running = false;
    bool commands_alloced = false;
    size_t expected_samples = 0;

    WakeCommand commands[MAX_COMMANDS];
    int num_commands = 0;
    wake_engine_cb_t callback = nullptr;
};

extern "C" {

wake_engine_t wake_engine_create(const char *language, int duration_ms, float threshold) {
    auto *impl = new WakeEngineImpl();
    impl->language = language ? language : "cn";
    impl->duration_ms = duration_ms > 0 ? duration_ms : 3000;
    impl->threshold = threshold > 0 ? threshold : 0.05f;
    return (wake_engine_t)impl;
}

void wake_engine_destroy(wake_engine_t engine) {
    auto *impl = (WakeEngineImpl *)engine;
    if (!impl) return;

    if (impl->commands_alloced) {
        esp_mn_commands_free();
        impl->commands_alloced = false;
    }
    if (impl->model_data && impl->multinet) {
        impl->multinet->destroy(impl->model_data);
    }
    if (impl->models) {
        esp_srmodel_deinit(impl->models);
    }
    delete impl;
}

int wake_engine_init(wake_engine_t engine) {
    auto *impl = (WakeEngineImpl *)engine;
    if (!impl) return -1;

    impl->models = esp_srmodel_init("model");
    if (!impl->models || impl->models->num == -1) {
        ESP_LOGE(TAG, "Failed to init sr models");
        return -1;
    }

    impl->mn_name = esp_srmodel_filter(impl->models, ESP_MN_PREFIX, impl->language.c_str());
    if (!impl->mn_name) {
        ESP_LOGW(TAG, "Language '%s' multinet not found, fallback", impl->language.c_str());
        impl->mn_name = esp_srmodel_filter(impl->models, ESP_MN_PREFIX, NULL);
    }
    if (!impl->mn_name) {
        ESP_LOGE(TAG, "No multinet model found");
        return -1;
    }

    impl->multinet = esp_mn_handle_from_name(impl->mn_name);
    impl->model_data = impl->multinet->create(impl->mn_name, impl->duration_ms);
    impl->multinet->set_det_threshold(impl->model_data, impl->threshold);
    impl->expected_samples = (size_t)impl->multinet->get_samp_chunksize(impl->model_data);

    esp_err_t cmd_ret = esp_mn_commands_alloc(impl->multinet, impl->model_data);
    if (cmd_ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to alloc multinet commands: %d", cmd_ret);
        return -1;
    }
    impl->commands_alloced = true;

    ESP_LOGI(TAG, "Multinet ready: %s, threshold=%.2f, duration=%dms, feed=%d samples",
             impl->mn_name, impl->threshold, impl->duration_ms, (int)impl->expected_samples);
    return 0;
}

void wake_engine_start(wake_engine_t engine) {
    auto *impl = (WakeEngineImpl *)engine;
    if (impl) impl->running = true;
}

void wake_engine_stop(wake_engine_t engine) {
    auto *impl = (WakeEngineImpl *)engine;
    if (impl) {
        impl->running = false;
        if (impl->model_data && impl->multinet) {
            impl->multinet->clean(impl->model_data);
        }
    }
}

void wake_engine_feed(wake_engine_t engine, const int16_t *mono_data, size_t samples) {
    auto *impl = (WakeEngineImpl *)engine;
    if (!impl || !impl->model_data || !impl->running) return;
    if (!mono_data || samples != impl->expected_samples) {
        ESP_LOGW(TAG, "Skip frame: samples=%d expected=%d",
                 (int)samples, (int)impl->expected_samples);
        return;
    }

    esp_mn_state_t state = impl->multinet->detect(impl->model_data, (int16_t *)mono_data);

    if (state == ESP_MN_STATE_DETECTING) {
        return;
    } else if (state == ESP_MN_STATE_DETECTED) {
        esp_mn_results_t *result = impl->multinet->get_results(impl->model_data);
        ESP_LOGI(TAG, "Detected state=%d num=%d text=%s",
                 result->state, result->num, result->string);
        for (int i = 0; i < result->num; i++) {
            int index = result->command_id[i] - 1;
            const char *active = esp_mn_commands_get_string(result->command_id[i]);
            ESP_LOGI(TAG, "Candidate[%d]: cmd_id=%d phrase_id=%d prob=%.3f active=%s",
                     i, result->command_id[i], result->phrase_id[i], result->prob[i],
                     active ? active : "(null)");
            if (index >= 0 && index < impl->num_commands) {
                ESP_LOGI(TAG, "Detected: %s (id=%d prob=%.3f)",
                         impl->commands[index].text.c_str(),
                         result->command_id[i], result->prob[i]);
                if (impl->callback) {
                    impl->callback(impl->commands[index].text.c_str());
                }
            }
        }
        impl->multinet->clean(impl->model_data);
    } else if (state == ESP_MN_STATE_TIMEOUT) {
        // ESP_LOGI(TAG, "Multinet timeout, clean state");
        impl->multinet->clean(impl->model_data);
    }
}

size_t wake_engine_get_feed_size(wake_engine_t engine) {
    auto *impl = (WakeEngineImpl *)engine;
    if (!impl || !impl->model_data || !impl->multinet) return 0;
    return impl->multinet->get_samp_chunksize(impl->model_data);
}

void wake_engine_set_callback(wake_engine_t engine, wake_engine_cb_t cb) {
    auto *impl = (WakeEngineImpl *)engine;
    if (impl) impl->callback = cb;
}

void wake_engine_clear_commands(wake_engine_t engine) {
    auto *impl = (WakeEngineImpl *)engine;
    if (!impl) return;
    impl->num_commands = 0;
    esp_err_t ret = esp_mn_commands_clear();
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "Clear commands failed: %d", ret);
    }
}

void wake_engine_add_command(wake_engine_t engine, const char *command, const char *text) {
    auto *impl = (WakeEngineImpl *)engine;
    if (!impl || impl->num_commands >= MAX_COMMANDS) return;

    impl->commands[impl->num_commands] = {command, text};
    esp_err_t add_ret = esp_mn_commands_add(impl->num_commands + 1, command);
    if (add_ret != ESP_OK) {
        ESP_LOGE(TAG, "Add command failed: %s -> %s, err=%d", command, text, add_ret);
        return;
    }
    impl->num_commands++;
    esp_mn_error_t *err = esp_mn_commands_update();
    if (err != NULL && err->num > 0) {
        ESP_LOGE(TAG, "Update commands has %d invalid phrase(s)", err->num);
        for (int i = 0; i < err->num; i++) {
            ESP_LOGE(TAG, "Invalid phrase[%d]: %s", i,
                     err->phrases[i] ? err->phrases[i]->string : "(null)");
        }
        return;
    }
    esp_mn_commands_print();
    esp_mn_active_commands_print();
    ESP_LOGI(TAG, "Added command: %s -> %s", command, text);
}

} /* extern "C" */
