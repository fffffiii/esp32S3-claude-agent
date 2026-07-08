#ifndef WAKE_ENGINE_H
#define WAKE_ENGINE_H

#include "esp_err.h"
#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef void* wake_engine_t;
typedef void (*wake_engine_cb_t)(const char *command);

wake_engine_t wake_engine_create(const char *language, int duration_ms, float threshold);
void          wake_engine_destroy(wake_engine_t engine);
int           wake_engine_init(wake_engine_t engine);
void          wake_engine_start(wake_engine_t engine);
void          wake_engine_stop(wake_engine_t engine);
void          wake_engine_feed(wake_engine_t engine, const int16_t *mono_data, size_t samples);
size_t        wake_engine_get_feed_size(wake_engine_t engine);
void          wake_engine_set_callback(wake_engine_t engine, wake_engine_cb_t cb);
void          wake_engine_clear_commands(wake_engine_t engine);
void          wake_engine_add_command(wake_engine_t engine, const char *command, const char *text);

#ifdef __cplusplus
}
#endif

#endif /* WAKE_ENGINE_H */
