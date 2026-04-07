// drumzio_calibrate.c - E-drum calibration firmware for Raspberry Pi Pico
// Uses USB CDC for reliable communication with PC.
// Sends hit data as JSON, receives config updates as JSON.
// Iterative calibration: PC guides phases (HEAD, RIM, BOTH).

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
#include "pico/stdlib.h"
#include "pico/time.h"
#include "hardware/adc.h"
#include "pico/stdio_usb.h"  // For USB CDC
#include "drum_trigger.h"

// JSON parsing helper (simple, assumes well-formed input)
int parse_json_int(const char *json, const char *key, int *value) {
    char search[64];
    sprintf(search, "\"%s\":", key);
    const char *pos = strstr(json, search);
    if (!pos) return 0;
    pos += strlen(search);
    while (*pos == ' ' || *pos == '\t') pos++;
    *value = atoi(pos);
    return 1;
}

int main() {
    // Init USB stdio (CDC)
    stdio_init_all();
    // Wait for USB connection
    while (!stdio_usb_connected()) {
        sleep_ms(100);
    }
    printf("{\"status\":\"ready\"}\n");

    // ADC init
    adc_init();
    adc_gpio_init(26);  // Head piezo
    adc_gpio_init(27);  // Rim piezo
    adc_set_temp_sensor_enabled(false);

    // Default config - EVEN LOWER for rimshots, more permissive BOTH ratio
    drum_trigger_cfg_t cfg = {
        .th_high_head = 300, .th_low_head = 250,  // Even lower
        .th_high_rim  = 300, .th_low_rim  = 250,  // Even lower
        .scan_min_ms = 10,
        .release_ms  = 30,
        .max_group_ms = 250,
        .retrigger_head_ms = 30,
        .retrigger_rim_ms  = 30,
        .both_ratio_q15 = 36044,  // 1.1 in Q15 - allows some variation between sensors
        .min_secondary_for_both = 200  // Lowered
    };

    drum_trigger_state_t st;
    drum_trigger_init(&st);

    char linebuf[512];
    int line_idx = 0;
    uint32_t last_debug_ms = 0;

    while (1) {
        // Read ADC (centered around 2048)
        adc_select_input(0);
        int32_t signal = (int32_t)(adc_read() - 2048);
        uint16_t rim = (uint16_t)abs(signal);

        adc_select_input(1);
        signal = (int32_t)(adc_read() - 2048);
        uint16_t head = (uint16_t)abs(signal);

        uint32_t now_ms = to_ms_since_boot(get_absolute_time());

        // Update trigger
        drum_hit_t hit = drum_trigger_update(&st, &cfg, head, rim, now_ms);

        // Send hit if detected
        if (hit.kind != DRUM_HIT_NONE) {
            const char *kind_str = (hit.kind == DRUM_HIT_HEAD) ? "HEAD" :
                                   (hit.kind == DRUM_HIT_RIM) ? "RIM" :
                                   (hit.kind == DRUM_HIT_BOTH) ? "BOTH" : "NONE";
            printf("{\"hit\":{\"kind\":\"%s\",\"peak_head\":%u,\"peak_rim\":%u,\"t_ms\":%u}}\n",
                   kind_str, hit.peak_head, hit.peak_rim, hit.t_ms);
            fflush(stdout);
        }

        // Debug output every 100ms
        if (now_ms - last_debug_ms >= 100) {
            printf("{\"debug\":{\"adc_head\":%u,\"adc_rim\":%u,\"group_active\":%d,\"peak_head\":%u,\"peak_rim\":%u,\"th_h\":%u,\"th_l\":%u,\"th_r\":%u,\"th_r_l\":%u}}\n",
                   head, rim, st.group_active ? 1 : 0, st.peak_head, st.peak_rim,
                   cfg.th_high_head, cfg.th_low_head, cfg.th_high_rim, cfg.th_low_rim);
            fflush(stdout);
            last_debug_ms = now_ms;
        }

        // Check for incoming USB data (config updates or commands)
        int c;
        while ((c = getchar_timeout_us(0)) != PICO_ERROR_TIMEOUT) {
            if (c == '\n' || c == '\r') {
                if (line_idx > 0) {
                    linebuf[line_idx] = '\0';
                    // Parse JSON config update
                    int val;
                    if (parse_json_int(linebuf, "th_high_head", &val)) cfg.th_high_head = val;
                    if (parse_json_int(linebuf, "th_low_head", &val)) cfg.th_low_head = val;
                    if (parse_json_int(linebuf, "th_high_rim", &val)) cfg.th_high_rim = val;
                    if (parse_json_int(linebuf, "th_low_rim", &val)) cfg.th_low_rim = val;
                    if (parse_json_int(linebuf, "scan_min_ms", &val)) cfg.scan_min_ms = val;
                    if (parse_json_int(linebuf, "release_ms", &val)) cfg.release_ms = val;
                    if (parse_json_int(linebuf, "max_group_ms", &val)) cfg.max_group_ms = val;
                    if (parse_json_int(linebuf, "retrigger_head_ms", &val)) cfg.retrigger_head_ms = val;
                    if (parse_json_int(linebuf, "retrigger_rim_ms", &val)) cfg.retrigger_rim_ms = val;
                    if (parse_json_int(linebuf, "both_ratio_q15", &val)) cfg.both_ratio_q15 = val;
                    if (parse_json_int(linebuf, "min_secondary_for_both", &val)) cfg.min_secondary_for_both = val;
                    // Send ack
                    printf("{\"status\":\"config_updated\"}\n");
                    fflush(stdout);
                }
                line_idx = 0;
            } else if (line_idx < sizeof(linebuf) - 1) {
                linebuf[line_idx++] = (char)c;
            }
        }

        tight_loop_contents();
    }

    return 0;
}