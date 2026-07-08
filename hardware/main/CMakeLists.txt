set(COMPONENT_SRCS
    "main.c"
    "display.c"
    "audio_service.c"
    "task_sound.c"
    "speech_player.c"
    "wake_engine.cc"
    "wifi_wrapper.cc"
    "whitebox_config.c"
    "whitebox_mqtt.c"
)
set(COMPONENT_ADD_INCLUDEDIRS .)
set(COMPONENT_REQUIRES mqtt esp_psram audio_pipeline audio_stream esp-adf-libs)
register_component()

spiffs_create_partition_image(storage ../spiffs FLASH_IN_PROJECT)
