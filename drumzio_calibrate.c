#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "pico/stdlib.h"
#include "hardware/adc.h"
#include "pico/stdio_usb.h"
#include "drum_trigger.h"

// Helper simple pour parser les nouvelles clés en US
int parse_json_int(const char *json, const char *key, int *value) {
    char search[64];
    sprintf(search, "\"%s\":", key);
    const char *pos = strstr(json, search);
    if (!pos) return 0;
    pos += strlen(search);
    while (*pos == ' ' || *pos == '\t' || *pos == ':') pos++;
    *value = atoi(pos);
    return 1;
}

int main() {
    stdio_init_all();
    while (!stdio_usb_connected()) { sleep_ms(100); }
    
    printf("{\"status\":\"ready\", \"info\":\"Pico 2 Ultra-Fast Trigger\"}\n");

    adc_init();
    adc_gpio_init(26); // Input 0
    adc_gpio_init(27); // Input 1

    // Nouvelle config par défaut adaptée au module rapide
    drum_trigger_cfg_t cfg = {
        .th_high_head = 500, .th_low_head = 450,
        .th_high_rim  = 600, .th_low_rim  = 550,
        .scan_min_us  = 1500,   // 1.5ms
        .retrigger_us = 30000,  // 30ms
        .crosstalk_min_us = 20000 // 20ms
    };

    drum_trigger_state_t st;
    drum_trigger_init(&st);

    char linebuf[512];
    int line_idx = 0;
    uint32_t last_debug_us = 0;

    while (1) {
        // 1. Lecture ADC ultra-rapide
        adc_select_input(1);
        uint16_t head = (uint16_t)abs((int32_t)adc_read() - 2048);
        adc_select_input(0);
        uint16_t rim = (uint16_t)abs((int32_t)adc_read() - 2048);

        // Dans le main, remplaçons le calcul du temps :
        uint64_t now_us_64 = to_us_since_boot(get_absolute_time());
        uint32_t now_us = (uint32_t)now_us_64; // Pour l'algo de détection (32 bits suffisent)


        // 2. Mise à jour de la détection
        drum_hit_t hit = drum_trigger_update(&st, &cfg, head, rim, now_us);

        // 3. Envoi des données de frappe (JSON)
        if (hit.kind != DRUM_HIT_NONE) {
            const char *kind_str = (hit.kind == DRUM_HIT_HEAD) ? "HEAD" : "RIM";
            // On utilise p_h, p_r et t_us pour coller au script Python
            printf("{\"hit\":{\"kind\":\"%s\",\"p_h\":%u,\"p_r\":%u,\"t_us\":%lu}}\n",
                kind_str, hit.p_h, hit.p_r, hit.t_us);
            fflush(stdout);
        }

        // 4. Debug périodique (toutes les 100ms)
        if (now_us - last_debug_us >= 100000) {
            printf("{\"debug\":{\"h\":%u,\"r\":%u,\"st\":%d,\"p_h\":%u,\"p_r\":%u}}\n",
                   head, rim, st.group_active ? 1 : 0, st.peak_head, st.peak_rim);
            fflush(stdout);
            last_debug_us = now_us;
        }

        // 5. Gestion des commandes USB (Config en temps réel)
        int c;
        while ((c = getchar_timeout_us(0)) != PICO_ERROR_TIMEOUT) {
            if (c == '\n' || c == '\r') {
                if (line_idx > 0) {
                    linebuf[line_idx] = '\0';
                    int val;
                    if (parse_json_int(linebuf, "th_high_head", &val)) cfg.th_high_head = val;
                    if (parse_json_int(linebuf, "th_low_head", &val))  cfg.th_low_head = val;
                    if (parse_json_int(linebuf, "th_high_rim", &val))  cfg.th_high_rim = val;
                    if (parse_json_int(linebuf, "th_low_rim", &val))   cfg.th_low_rim = val;
                    if (parse_json_int(linebuf, "scan_min_us", &val))  cfg.scan_min_us = val;
                    if (parse_json_int(linebuf, "retrigger_us", &val)) cfg.retrigger_us = val;
                    if (parse_json_int(linebuf, "crosstalk_min_us", &val)) cfg.crosstalk_min_us = val;
                    
                    printf("{\"status\":\"config_updated\"}\n");
                    fflush(stdout);
                }
                line_idx = 0;
            } else if (line_idx < sizeof(linebuf) - 1) {
                linebuf[line_idx++] = (char)c;
            }
        }
    }
    return 0;
}