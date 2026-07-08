#ifndef TASK_SOUND_H
#define TASK_SOUND_H

#include "esp_err.h"
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    TASK_SOUND_DONE,
    TASK_SOUND_PERMISSION_WAIT,
    TASK_SOUND_ACTION_CONFIRM,
} task_sound_type_t;

/**
 * @brief Initialize task sound module (ADF pipeline + playback task).
 *        Must be called after audio_service_init() and SPIFFS mount.
 */
esp_err_t task_sound_init(void);

/**
 * @brief Play a notification sound. Interrupts any currently playing sound.
 */
esp_err_t task_sound_play(task_sound_type_t type);

/**
 * @brief Stop currently playing sound.
 */
void task_sound_stop(void);

#ifdef __cplusplus
}
#endif

#endif /* TASK_SOUND_H */
