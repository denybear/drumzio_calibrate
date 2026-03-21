#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "hardware/adc.h"

#include "drum_trigger.h"

/*
Petite astuce de réglage “rapide”

Si tu vois des hits parasites au repos → monte th_high_* (seuil haut). [edrums.github.io]
Si tu vois des doubles hits → monte retrigger_*_ms ou release_ms (mask/retrigger). [support.dwdrums.com]
Si tu rates des roulements → baisse retrigger_*_ms et release_ms.
*/


// --------------------
// Choisis ton sampling
// --------------------
#ifndef SAMPLE_RATE_HZ
#define SAMPLE_RATE_HZ 5000  // 5000 ou 10000
#endif

#define SAMPLE_PERIOD_US (1000000u / SAMPLE_RATE_HZ)

// Durées calibration
#define BASELINE_SECONDS 3
#define REPORT_MS        200

static inline uint32_t now_ms(void) {
    return to_ms_since_boot(get_absolute_time());
}

static inline uint16_t read_adc_channel(uint ch) {
    adc_select_input(ch);
    // adc_read() renvoie 12 bits (0..4095) [1](https://picodocs.pinout.xyz/group__hardware__adc.html)
    return adc_read();
}

static inline uint16_t u16_max(uint16_t a, uint16_t b) { return a > b ? a : b; }
static inline uint16_t u16_min(uint16_t a, uint16_t b) { return a < b ? a : b; }

// Ratio Q15 = (max/min)<<15
static inline uint32_t ratio_q15(uint16_t maxv, uint16_t minv) {
    if (minv == 0) return 0xFFFFFFFFu;
    return ((uint32_t)maxv << 15) / (uint32_t)minv;
}

// Impression d'un hit
static void print_hit(const drum_hit_t *h) {
    const char *k = "NONE";
    if (h->kind == DRUM_HIT_HEAD) k = "HEAD";
    else if (h->kind == DRUM_HIT_RIM) k = "RIM";
    else if (h->kind == DRUM_HIT_BOTH) k = "BOTH";

    uint16_t maxv = (h->peak_head >= h->peak_rim) ? h->peak_head : h->peak_rim;
    uint16_t minv = (h->peak_head >= h->peak_rim) ? h->peak_rim : h->peak_head;
    uint32_t r = ratio_q15(maxv, minv);

    printf("[HIT] t=%lu ms  kind=%s  peak_head=%u  peak_rim=%u  ratio(max/min)=%.3f\n",
           (unsigned long)h->t_ms, k, h->peak_head, h->peak_rim, (float)r / 32768.0f);
}

int main() {
    stdio_init_all();
    sleep_ms(1200);
    printf("\n=== eDrum calibration tool (ADC0/ADC1) ===\n");
    printf("Sample rate: %u Hz  (period=%u us)\n", SAMPLE_RATE_HZ, SAMPLE_PERIOD_US);
    printf("Instructions:\n");
    printf("  1) Ne frappe rien pendant %d s (baseline).\n", BASELINE_SECONDS);
    printf("  2) Ensuite frappe HEAD puis RIM plusieurs fois.\n");
    printf("  3) Observe les suggestions de seuils et de crosstalk.\n\n");

    // ADC init
    adc_init();
    adc_gpio_init(26); // ADC0 = GPIO26 [1](https://picodocs.pinout.xyz/group__hardware__adc.html)
    adc_gpio_init(27); // ADC1 = GPIO27 [1](https://picodocs.pinout.xyz/group__hardware__adc.html)

    // -----------------------------
    // 1) Baseline (bruit au repos)
    // -----------------------------
    uint32_t t0 = now_ms();
    uint32_t t_end = t0 + (BASELINE_SECONDS * 1000u);

    uint32_t sum_h = 0, sum_r = 0;
    uint32_t n = 0;
    uint16_t min_h = 4095, min_r = 4095;
    uint16_t max_h = 0,    max_r = 0;

    while (now_ms() < t_end) {
        uint16_t h = read_adc_channel(0);
        uint16_t r = read_adc_channel(1);

        sum_h += h; sum_r += r; n++;
        min_h = u16_min(min_h, h); max_h = u16_max(max_h, h);
        min_r = u16_min(min_r, r); max_r = u16_max(max_r, r);

        sleep_us(SAMPLE_PERIOD_US);
    }

    uint16_t mean_h = (uint16_t)(sum_h / n);
    uint16_t mean_r = (uint16_t)(sum_r / n);

    uint16_t noise_h = max_h - min_h;
    uint16_t noise_r = max_r - min_r;

    printf("Baseline done (%lu samples)\n", (unsigned long)n);
    printf("  HEAD: mean=%u  min=%u  max=%u  noise_span=%u\n", mean_h, min_h, max_h, noise_h);
    printf("  RIM : mean=%u  min=%u  max=%u  noise_span=%u\n", mean_r, min_r, max_r, noise_r);

    // Suggestions de seuils (grossières mais utiles pour démarrer)
    // marges: 3x bruit + offset
    uint16_t margin_h = (uint16_t)(noise_h * 3u + 30u);
    uint16_t margin_r = (uint16_t)(noise_r * 3u + 30u);

    uint16_t sug_low_h  = (uint16_t)(max_h + (margin_h / 2u));
    uint16_t sug_high_h = (uint16_t)(max_h + margin_h);

    uint16_t sug_low_r  = (uint16_t)(max_r + (margin_r / 2u));
    uint16_t sug_high_r = (uint16_t)(max_r + margin_r);

    if (sug_high_h > 4095) sug_high_h = 4095;
    if (sug_high_r > 4095) sug_high_r = 4095;
    if (sug_low_h  > 4095) sug_low_h  = 4095;
    if (sug_low_r  > 4095) sug_low_r  = 4095;

    printf("\nSuggested thresholds (start point):\n");
    printf("  HEAD: th_low=%u  th_high=%u\n", sug_low_h, sug_high_h);
    printf("  RIM : th_low=%u  th_high=%u\n", sug_low_r, sug_high_r);

    // -----------------------------
    // 2) Mise en place du trigger
    // -----------------------------
    drum_trigger_cfg_t cfg = {
        .th_high_head = sug_high_h,
        .th_low_head  = sug_low_h,
        .th_high_rim  = sug_high_r,
        .th_low_rim   = sug_low_r,

        // Valeurs recommandées selon 5 kHz / 10 kHz (on garde 2ms scan)
        .scan_min_ms  = 2,
        .release_ms   = (SAMPLE_RATE_HZ >= 10000) ? 3 : 4,
        .max_group_ms = 30,

        .retrigger_head_ms = (SAMPLE_RATE_HZ >= 10000) ? 15 : 18,
        .retrigger_rim_ms  = (SAMPLE_RATE_HZ >= 10000) ? 15 : 18,

        .both_ratio_q15 = drum_q15_from_float(1.50f),
        .min_secondary_for_both = (uint16_t)( (sug_high_h + sug_high_r) / 2u )
    };

    drum_trigger_state_t st;
    drum_trigger_init(&st);

    printf("\nTrigger config (initial):\n");
    printf("  scan_min_ms=%lu release_ms=%lu retrigger_head=%lu retrigger_rim=%lu\n",
           (unsigned long)cfg.scan_min_ms,
           (unsigned long)cfg.release_ms,
           (unsigned long)cfg.retrigger_head_ms,
           (unsigned long)cfg.retrigger_rim_ms);
    printf("  both_ratio=%.2f  min_secondary_for_both=%u\n\n",
           (float)cfg.both_ratio_q15 / 32768.0f, cfg.min_secondary_for_both);

    // -----------------------------
    // 3) Boucle de calibration: affichage périodique + hits
    // -----------------------------
    uint32_t last_report = now_ms();

    // Stats crosstalk / rimshot
    uint32_t hit_count = 0;
    uint32_t head_dom = 0, rim_dom = 0, both_cnt = 0;

    // Estimation simple du crosstalk : ratio moyen secondaire/principal
    // On accumule les ratios quand HEAD domine et quand RIM domine
    uint32_t sum_xtalk_head_q15 = 0, n_xtalk_head = 0;
    uint32_t sum_xtalk_rim_q15  = 0, n_xtalk_rim  = 0;

    while (true) {
        uint16_t head = read_adc_channel(0);
        uint16_t rim  = read_adc_channel(1);

        drum_hit_t hit = drum_trigger_update(&st, &cfg, head, rim, now_ms());
        if (hit.kind != DRUM_HIT_NONE) {
            hit_count++;
            print_hit(&hit);

            // Dominance + xtalk stats
            if (hit.kind == DRUM_HIT_HEAD) {
                head_dom++;
                // xtalk = rim/head en Q15 (si head>0)
                if (hit.peak_head > 0) {
                    sum_xtalk_head_q15 += ((uint32_t)hit.peak_rim << 15) / (uint32_t)hit.peak_head;
                    n_xtalk_head++;
                }
            } else if (hit.kind == DRUM_HIT_RIM) {
                rim_dom++;
                if (hit.peak_rim > 0) {
                    sum_xtalk_rim_q15 += ((uint32_t)hit.peak_head << 15) / (uint32_t)hit.peak_rim;
                    n_xtalk_rim++;
                }
            } else {
                both_cnt++;
            }
        }

        // Report périodique (bruit courant, et recommandations)
        uint32_t now = now_ms();
        if ((uint32_t)(now - last_report) >= REPORT_MS) {
            last_report = now;

            float xt_head = (n_xtalk_head ? (float)sum_xtalk_head_q15 / (float)n_xtalk_head / 32768.0f : 0.0f);
            float xt_rim  = (n_xtalk_rim  ? (float)sum_xtalk_rim_q15  / (float)n_xtalk_rim  / 32768.0f : 0.0f);

            // Recommandation min_secondary_for_both :
            // si xtalk moyen ~0.25, mets min_secondary ~ 2x l’xtalk attendu * pic typique.
            // Ici on reste conservateur : base sur seuil haut actuel.
            uint16_t suggested_min_secondary = (uint16_t)u16_max(cfg.th_high_head, cfg.th_high_rim);

            // Recommandation both_ratio:
            // - si beaucoup de BOTH => ratio trop permissif -> descendre
            // - si jamais de BOTH => peut-être remonter
            float both_ratio = (float)cfg.both_ratio_q15 / 32768.0f;

            printf("\n[STAT] hits=%lu  head=%lu  rim=%lu  both=%lu\n",
                   (unsigned long)hit_count, (unsigned long)head_dom, (unsigned long)rim_dom, (unsigned long)both_cnt);
            printf("[STAT] xtalk avg: rim_when_head=%.2f  head_when_rim=%.2f (secondary/primary)\n",
                   xt_head, xt_rim);

            printf("[SUGG] th_high_head=%u th_low_head=%u | th_high_rim=%u th_low_rim=%u\n",
                   cfg.th_high_head, cfg.th_low_head, cfg.th_high_rim, cfg.th_low_rim);
            printf("[SUGG] min_secondary_for_both >= ~%u (start) ; both_ratio=%.2f (1.3 strict, 1.7 permissif)\n",
                   suggested_min_secondary, both_ratio);

            printf("[TIP ] Si doubles triggers -> +retrigger_ms ou +release_ms ; si roulements ratés -> -retrigger_ms/-release_ms\n");
        }

        sleep_us(SAMPLE_PERIOD_US);
    }
}
