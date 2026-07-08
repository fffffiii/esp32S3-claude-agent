#include "chaintic_wake_word.h"

#include <string>
#include <deque>
#include <vector>
#include <mutex>
#include <condition_variable>
#include <atomic>
#include <memory>
#include <string.h>

#include <esp_log.h>
#include <esp_timer.h>
#include <esp_mn_iface.h>
#include <esp_mn_models.h>
#include <esp_mn_speech_commands.h>
#include <cJSON.h>
#define TAG "ChainticWakeWord"
#define DETECTION_RUNNING_EVENT 1

struct CCommand {
  std::string command;
  std::string text;
  std::string action;
};

class ChainticWakeWord {
public:
  ChainticWakeWord() {
    event_group_ = xEventGroupCreate();
  }
  ~ChainticWakeWord() {
    if (multinet_model_data_ != nullptr && multinet_ != nullptr) {
      multinet_->destroy(multinet_model_data_);
      multinet_model_data_ = nullptr;
    }
    if (models_ != nullptr) {
      esp_srmodel_deinit(models_);
    }
    if (afe_data_ != nullptr) {
      afe_iface_->destroy(afe_data_);
      afe_data_ = nullptr;
    }
    if (event_group_ != nullptr) {
      vEventGroupDelete(event_group_);
      event_group_ = nullptr;
    }
  }

  bool Initialize(bool aec_enable, srmodel_list_t* models_list) {
    aec_enable_ = aec_enable;
    Initialize_afe(models_list);
    if (models_list == nullptr) {
      language_ = "cn";
      models_ = esp_srmodel_init("model");
    } else {
      models_ = models_list;
    }
    if (models_ == nullptr || models_->num == -1) {
      ESP_LOGE(TAG, "Failed to initialize wakenet model");
      return false;
    }
    mn_name_ = esp_srmodel_filter(models_, ESP_MN_PREFIX, language_.c_str());
    if (mn_name_ == nullptr) {
      ESP_LOGW(TAG, "Language '%s' multinet not found, fallback", language_.c_str());
      mn_name_ = esp_srmodel_filter(models_, ESP_MN_PREFIX, NULL);
    }
    if (mn_name_ == nullptr) {
      ESP_LOGE(TAG, "Failed to initialize multinet, mn_name is nullptr");
      return false;
    }
    multinet_ = esp_mn_handle_from_name(mn_name_);
    multinet_model_data_ = multinet_->create(mn_name_, duration_);
    multinet_->set_det_threshold(multinet_model_data_, threshold_);
    esp_mn_commands_clear();
    commands_[0] = {"xiao lian", "你好chaintic", "wake"};
    commands_size_++;
    for (size_t i = 0; i < commands_size_; i++) {
      esp_mn_commands_add((int)i + 1, commands_[i].command.c_str());
    }
    esp_mn_commands_update();
    // multinet_->print_active_speech_commands(multinet_model_data_);
    return true;
  }

  void OnWakeWordDetected(std::function<void(const std::string& wake_word,
                                             const std::string& action,
                                             int command_id,
                                             float prob)> callback) {
    wake_word_detected_callback_ = callback;
  }

  void Start() {
    running_ = true;
    xEventGroupSetBits(event_group_, DETECTION_RUNNING_EVENT);
  }
  void Stop() {
    running_ = false;
    xEventGroupClearBits(event_group_, DETECTION_RUNNING_EVENT);
    if (afe_data_ != nullptr) {
      afe_iface_->reset_buffer(afe_data_);
    }
  }

  void Feed(const int16_t* data, size_t samples) {
    if (afe_data_ == nullptr) {
      return;
    }
    afe_iface_->feed(afe_data_, data);
  }
  size_t GetFeedSize() {
    if (multinet_model_data_ == nullptr) {
      return 0;
    }
    return multinet_->get_samp_chunksize(multinet_model_data_);
  }


  void AddCommand(const char* command, const char* text, const char* action) {
    if (commands_size_ < MAX_COMMANDS) {
      commands_[commands_size_] = {command, text, action ? action : "wake"};
      esp_mn_commands_add((int)commands_size_ + 1, commands_[commands_size_].command.c_str());
      commands_size_++;
      esp_mn_commands_update();
      // esp_mn_commands_print();
    } else {
      ESP_LOGE(TAG, "Command list is full");
    }
  }
  void ClearCommands() { commands_size_ = 0; esp_mn_commands_clear();}
  void RemoveWakeWord(const char* wake_word) { esp_mn_commands_remove(wake_word); }

  void SetLangDurThresh(const char* lang, int dur_ms, float thresh) {
    if (lang) language_ = lang;
    if (dur_ms > 0) duration_ = dur_ms;
    if (thresh > 0) threshold_ = thresh;
  }

  void SetCallbackContext(chaintic_wake_engine_t engine,
                          const chaintic_wake_event_handler_t& event,
                          chaintic_wake_user_data_t user_data) {
    engine_ = engine;
    event_ = event;
    user_data_ = user_data;
  }

private:
  void Initialize_afe(srmodel_list_t* models_list) {
    int ref_num = aec_enable_ ? 1 : 0;
    if (ref_num == 0) {
      ESP_LOGI(TAG, "No AFE init, codec_->input_reference() is false");
      return;
    }
    if (models_list == nullptr) {
      models_ = esp_srmodel_init("model");
    } else {
      models_ = models_list;
    }
    std::string input_format = "MR";
    afe_config_t* afe_config = afe_config_init(input_format.c_str(), models_, AFE_TYPE_SR, AFE_MODE_HIGH_PERF);
    afe_config->aec_init = aec_enable_;
    afe_config->aec_mode = AEC_MODE_SR_HIGH_PERF;
    afe_config->afe_perferred_core = 1;
    afe_config->afe_perferred_priority = 1;
    afe_config->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;
    afe_iface_ = esp_afe_handle_from_config(afe_config);
    afe_data_ = afe_iface_->create_from_config(afe_config);

    xTaskCreate([](void* arg) {
      auto this_ = (ChainticWakeWord*)arg;
      this_->AudioDetectionTask();
      vTaskDelete(NULL);
    }, "audio_detection", 4096, this, 3, nullptr);
  }
  void AudioDetectionTask() {
    auto fetch_size = afe_iface_->get_fetch_chunksize(afe_data_);
    auto feed_size = afe_iface_->get_feed_chunksize(afe_data_);

    while (true) {
      xEventGroupWaitBits(event_group_, DETECTION_RUNNING_EVENT, pdFALSE, pdTRUE, portMAX_DELAY);
      auto res = afe_iface_->fetch_with_delay(afe_data_, portMAX_DELAY);
      if (res == nullptr || res->ret_value == ESP_FAIL) {
   
        continue;
      }
      if (event_.on_pcm_chunk && res->data && res->data_size >= (int)sizeof(int16_t)) {
        size_t samples = (size_t)res->data_size / sizeof(int16_t);
        chaintic_wake_callback_context_t ctx{engine_, user_data_};
        event_.on_pcm_chunk(&ctx,
                            reinterpret_cast<const int16_t*>(res->data),
                            samples);
      }
  
      if (res->data && res->data_size >= (int)sizeof(int16_t)) {
        int16_t sample0 = reinterpret_cast<const int16_t*>(res->data)[0];

      }
      auto mn_state = multinet_->detect(multinet_model_data_, res->data);

      if (mn_state == ESP_MN_STATE_DETECTING) {

        continue;
      } else if (mn_state == ESP_MN_STATE_DETECTED) {
        esp_mn_results_t* mn_result = multinet_->get_results(multinet_model_data_);

        for (int i = 0; i < mn_result->num && (xEventGroupGetBits(event_group_) & DETECTION_RUNNING_EVENT); i++) {
          int index = mn_result->command_id[i] - 1;
          if (index >= 0) {
            auto& command = commands_[index];
            if (wake_word_detected_callback_) {
              wake_word_detected_callback_(command.text, command.action, mn_result->command_id[i], mn_result->prob[i]);
            }
            ESP_LOGI(TAG, "Detected id=%d string=%s prob=%.3f map cmd=%s text=%s action=%s",
                     mn_result->command_id[i], mn_result->string, mn_result->prob[i],
                     command.command.c_str(), command.text.c_str(), command.action.c_str());
          } else {

          }
        }
        multinet_->clean(multinet_model_data_);
      } else if (mn_state == ESP_MN_STATE_TIMEOUT) {
        multinet_->clean(multinet_model_data_);
      }
    }
  }

private:
  static constexpr size_t MAX_COMMANDS = 16;
  size_t commands_size_ = 0;
  CCommand commands_[MAX_COMMANDS];

  esp_mn_iface_t* multinet_ = nullptr;
  model_iface_data_t* multinet_model_data_ = nullptr;
  srmodel_list_t* models_ = nullptr;
  char* mn_name_ = nullptr;
  std::string language_ = "cn";
  int duration_ = 3000;
  float threshold_ = 0.2f;

  EventGroupHandle_t event_group_ = nullptr;
  esp_afe_sr_iface_t* afe_iface_ = nullptr;
  esp_afe_sr_data_t* afe_data_ = nullptr;

  std::function<void(const std::string& wake_word, const std::string& action, int command_id, float prob)> wake_word_detected_callback_;
  std::atomic<bool> running_ = false;
  bool aec_enable_ = false;

  chaintic_wake_engine_t engine_ = nullptr;
  chaintic_wake_event_handler_t event_{};
  chaintic_wake_user_data_t user_data_ = nullptr;

};

struct chaintic_wake_engine_impl {
  ChainticWakeWord* cpp;
  chaintic_wake_event_handler_t event;
  chaintic_wake_user_data_t user_data;
};

extern "C" {
chaintic_wake_engine_t chaintic_wake_create_engine(const chaintic_wake_config_t* cfg) {
  auto* impl = new chaintic_wake_engine_impl();
  impl->cpp = new ChainticWakeWord();
  impl->event = cfg ? cfg->event_handler : chaintic_wake_event_handler_t{};
  impl->user_data = cfg ? cfg->user_data : nullptr;
  if (cfg) {
    impl->cpp->SetCallbackContext((chaintic_wake_engine_t)impl, impl->event, impl->user_data);
    impl->cpp->SetLangDurThresh(cfg->language, cfg->duration_ms, cfg->threshold);
    if (!impl->cpp->Initialize(cfg->aec_enable, cfg->models_list)) {
      delete impl->cpp;
      delete impl;
      return nullptr;
    }
  }
  return (chaintic_wake_engine_t)impl;
}

void chaintic_wake_destroy_engine(chaintic_wake_engine_t engine) {
  auto* impl = (chaintic_wake_engine_impl*)engine;
  if (!impl) return;
  delete impl->cpp;
  delete impl;
}

int chaintic_wake_init(chaintic_wake_engine_t engine) {
  auto* impl = (chaintic_wake_engine_impl*)engine;
  if (!impl) return -1;
  impl->cpp->OnWakeWordDetected([impl](const std::string& text,
                                       const std::string& action,
                                       int command_id,
                                       float prob) {
    if (impl->event.on_wake_detected) {
      chaintic_wake_callback_context_t ctx{(chaintic_wake_engine_t)impl, impl->user_data};
      impl->event.on_wake_detected(&ctx, text.c_str(), action.c_str(), command_id, prob);
    }
  });
  return 0;
}

void chaintic_wake_start(chaintic_wake_engine_t engine) {
  auto* impl = (chaintic_wake_engine_impl*)engine;
  if (!impl) return;
  impl->cpp->Start();
}
void chaintic_wake_stop(chaintic_wake_engine_t engine) {
  auto* impl = (chaintic_wake_engine_impl*)engine;
  if (!impl) return;
  impl->cpp->Stop();
}
void chaintic_wake_feed(chaintic_wake_engine_t engine, const int16_t* data, size_t samples) {
  auto* impl = (chaintic_wake_engine_impl*)engine;
  if (!impl) return;
  impl->cpp->Feed(data, samples);
}
size_t chaintic_wake_get_feed_size(chaintic_wake_engine_t engine) {
  auto* impl = (chaintic_wake_engine_impl*)engine;
  if (!impl) return 0;
  return impl->cpp->GetFeedSize();
}
void chaintic_wake_add_command(chaintic_wake_engine_t engine, const char* command, const char* text, const char* action) {
  auto* impl = (chaintic_wake_engine_impl*)engine;
  if (!impl) return;
  impl->cpp->AddCommand(command, text, action);
}
void chaintic_wake_add_wake_command(chaintic_wake_engine_t engine, const char* command, const char* text) {
  auto* impl = (chaintic_wake_engine_impl*)engine;
  if (!impl) return;
  impl->cpp->AddCommand(command, text, "wake");
}
void chaintic_wake_add_break_command(chaintic_wake_engine_t engine, const char* command, const char* text) {
  auto* impl = (chaintic_wake_engine_impl*)engine;
  if (!impl) return;
  impl->cpp->AddCommand(command, text, "break");
}
void chaintic_wake_clear_commands(chaintic_wake_engine_t engine) {
  auto* impl = (chaintic_wake_engine_impl*)engine;
  if (!impl) return;
  impl->cpp->ClearCommands();
}
void chaintic_wake_remove_command(chaintic_wake_engine_t engine, const char* wake_word) {
  auto* impl = (chaintic_wake_engine_impl*)engine;
  if (!impl) return;
  impl->cpp->RemoveWakeWord(wake_word);
}
}
