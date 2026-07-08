#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    const char* name;
    const uint8_t* start;
    const uint8_t* end;
} LocalMusicTrack;

extern const uint8_t awt_1_mp3_start[] asm("_binary_awt_1_mp3_start");
extern const uint8_t awt_1_mp3_end[]   asm("_binary_awt_1_mp3_end");
extern const uint8_t awt_2_mp3_start[] asm("_binary_awt_2_mp3_start");
extern const uint8_t awt_2_mp3_end[]   asm("_binary_awt_2_mp3_end");
extern const uint8_t awt_3_mp3_start[] asm("_binary_awt_3_mp3_start");
extern const uint8_t awt_3_mp3_end[]   asm("_binary_awt_3_mp3_end");
extern const uint8_t awt_4_mp3_start[] asm("_binary_awt_4_mp3_start");
extern const uint8_t awt_4_mp3_end[]   asm("_binary_awt_4_mp3_end");
extern const uint8_t awt_5_mp3_start[] asm("_binary_awt_5_mp3_start");
extern const uint8_t awt_5_mp3_end[]   asm("_binary_awt_5_mp3_end");

extern const uint8_t connecting_network_mp3_start[] asm("_binary_connecting_network_mp3_start");
extern const uint8_t connecting_network_mp3_end[]   asm("_binary_connecting_network_mp3_end");
extern const uint8_t kaiji_mp3_start[] asm("_binary_kaiji_mp3_start");
extern const uint8_t kaiji_mp3_end[]   asm("_binary_kaiji_mp3_end");
extern const uint8_t ota_ing_mp3_start[] asm("_binary_ota_ing_mp3_start");
extern const uint8_t ota_ing_mp3_end[]   asm("_binary_ota_ing_mp3_end");

extern const uint8_t plg_1_mp3_start[] asm("_binary_plg_1_mp3_start");
extern const uint8_t plg_1_mp3_end[]   asm("_binary_plg_1_mp3_end");
extern const uint8_t plg_2_mp3_start[] asm("_binary_plg_2_mp3_start");
extern const uint8_t plg_2_mp3_end[]   asm("_binary_plg_2_mp3_end");
extern const uint8_t plg_3_mp3_start[] asm("_binary_plg_3_mp3_start");
extern const uint8_t plg_3_mp3_end[]   asm("_binary_plg_3_mp3_end");
extern const uint8_t plg_4_mp3_start[] asm("_binary_plg_4_mp3_start");
extern const uint8_t plg_4_mp3_end[]   asm("_binary_plg_4_mp3_end");
extern const uint8_t plg_5_mp3_start[] asm("_binary_plg_5_mp3_start");
extern const uint8_t plg_5_mp3_end[]   asm("_binary_plg_5_mp3_end");
extern const uint8_t plg_6_mp3_start[] asm("_binary_plg_6_mp3_start");
extern const uint8_t plg_6_mp3_end[]   asm("_binary_plg_6_mp3_end");
extern const uint8_t plg_7_mp3_start[] asm("_binary_plg_7_mp3_start");
extern const uint8_t plg_7_mp3_end[]   asm("_binary_plg_7_mp3_end");
extern const uint8_t plg_8_mp3_start[] asm("_binary_plg_8_mp3_start");
extern const uint8_t plg_8_mp3_end[]   asm("_binary_plg_8_mp3_end");

extern const uint8_t power_low_mp3_start[] asm("_binary_power_low_mp3_start");
extern const uint8_t power_low_mp3_end[]   asm("_binary_power_low_mp3_end");

static const LocalMusicTrack kLocalMusicTracks[] = {
    { "awt-1", awt_1_mp3_start, awt_1_mp3_end },
    { "awt-2", awt_2_mp3_start, awt_2_mp3_end },
    { "awt-3", awt_3_mp3_start, awt_3_mp3_end },
    { "awt-4", awt_4_mp3_start, awt_4_mp3_end },
    { "awt-5", awt_5_mp3_start, awt_5_mp3_end },
    { "connecting-network", connecting_network_mp3_start, connecting_network_mp3_end },
    { "kaiji", kaiji_mp3_start, kaiji_mp3_end },
    { "ota-ing", ota_ing_mp3_start, ota_ing_mp3_end },
    { "plg-1", plg_1_mp3_start, plg_1_mp3_end },
    { "plg-2", plg_2_mp3_start, plg_2_mp3_end },
    { "plg-3", plg_3_mp3_start, plg_3_mp3_end },
    { "plg-4", plg_4_mp3_start, plg_4_mp3_end },
    { "plg-5", plg_5_mp3_start, plg_5_mp3_end },
    { "plg-6", plg_6_mp3_start, plg_6_mp3_end },
    { "plg-7", plg_7_mp3_start, plg_7_mp3_end },
    { "plg-8", plg_8_mp3_start, plg_8_mp3_end },
    { "power-low", power_low_mp3_start, power_low_mp3_end },
};

static inline size_t local_music_track_count(void) {
    return sizeof(kLocalMusicTracks) / sizeof(kLocalMusicTracks[0]);
}

static inline const LocalMusicTrack* local_music_select_by_name(const char* name) {
    if (!name) return NULL;
    for (size_t i = 0; i < local_music_track_count(); ++i) {
        if (kLocalMusicTracks[i].name && strcmp(kLocalMusicTracks[i].name, name) == 0) {
            return &kLocalMusicTracks[i];
        }
    }
    return NULL;
}

#ifdef __cplusplus
}
#endif