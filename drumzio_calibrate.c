#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <inttypes.h>
#include "pico/stdlib.h"
#include "pico/time.h"
#include "hardware/adc.h"

#include "drum_trigger.h"


bool parse_cfg_from_json_line(const char *line, drum_trigger_cfg_t *cfg, char *errbuf, size_t errlen) {
    // initialiser avec valeurs actuelles si besoin
    // recherche simple de chaque clé
    int found = 0;
    const char *p = line;
    #define TRY_PARSE(key, fmt, dest) do { \
        const char *k = strstr(p, key); \
        if (k) { if (sscanf(k + strlen(key), fmt, dest) == 1) found++; } \
    } while(0)

    TRY_PARSE("\"th_high_head\"", ":%d", &cfg->th_high_head);
    TRY_PARSE("\"th_low_head\"", ":%d", &cfg->th_low_head);
    TRY_PARSE("\"th_high_rim\"", ":%d", &cfg->th_high_rim);
    TRY_PARSE("\"th_low_rim\"", ":%d", &cfg->th_low_rim);
    TRY_PARSE("\"scan_min_ms\"", ":%d", &cfg->scan_min_ms);
    TRY_PARSE("\"release_ms\"", ":%d", &cfg->release_ms);
    TRY_PARSE("\"max_group_ms\"", ":%d", &cfg->max_group_ms);
    TRY_PARSE("\"retrigger_head_ms\"", ":%d", &cfg->retrigger_head_ms);
    TRY_PARSE("\"retrigger_rim_ms\"", ":%d", &cfg->retrigger_rim_ms);
    TRY_PARSE("\"both_ratio_q15\"", ":%d", &cfg->both_ratio_q15);
    TRY_PARSE("\"min_secondary_for_both\"", ":%d", &cfg->min_secondary_for_both);
    #undef TRY_PARSE

    if (found < 6) { // heuristique: attendre au moins quelques champs
        snprintf(errbuf, errlen, "missing fields (%d found)", found);
        return false;
    }
    // validation minimale
    if (!(cfg->th_low_head > 0 && cfg->th_low_head <= 4095 && cfg->th_high_head > cfg->th_low_head)) {
        snprintf(errbuf, errlen, "th_head invalid");
        return false;
    }
    if (!(cfg->th_low_rim > 0 && cfg->th_low_rim <= 4095 && cfg->th_high_rim > cfg->th_low_rim)) {
        snprintf(errbuf, errlen, "th_rim invalid");
        return false;
    }
    return true;
}


// appelle quand tu as un hit prêt à envoyer
void send_hit_csv(uint64_t t_us, int peak_head, int peak_rim, float dur_head_ms, float dur_rim_ms, const char *label) {
    if (label && label[0]) {
        printf("%" PRIu64 ",%d,%d,%.2f,%.2f,%s\n", t_us, peak_head, peak_rim, dur_head_ms, dur_rim_ms, label);
    } else {
        printf("%" PRIu64 ",%d,%d,%.2f,%.2f\n", t_us, peak_head, peak_rim, dur_head_ms, dur_rim_ms);
    }
    fflush(stdout);
}


// lit jusqu'à '\n' dans buf (taille buflen), retourne 1 si ligne reçue, 0 si timeout
bool read_line_uart(char *buf, size_t buflen, uint32_t timeout_ms) {
    size_t idx = 0;
    uint32_t start = to_ms_since_boot(get_absolute_time());
    while (1) {
        int c = getchar_timeout_us(0); // non bloquant
        if (c == PICO_ERROR_TIMEOUT) {
            // pas de data maintenant
        } else {
            if (c == '\r') continue;
            if (c == '\n') {
                buf[idx < buflen ? idx : (buflen-1)] = '\0';
                return true;
            }
            if (idx + 1 < buflen) buf[idx++] = (char)c;
        }
        if (to_ms_since_boot(get_absolute_time()) - start > timeout_ms) break;
        sleep_ms(1);
    }
    return false;
}


void handle_incoming_config(const char *line, drum_trigger_cfg_t *current_cfg) {
    char err[128];
    drum_trigger_cfg_t newcfg;

    printf("IN_RAW:%s\n", line);
    fflush(stdout);

    if (!parse_cfg_from_json_line(line, &newcfg, err, sizeof(err))) {
        printf("ERR %s\n", err);
        fflush(stdout);
        return;
    }
    // Optionnel: tester la config (ex: valeurs limites)
    memcpy(current_cfg, &newcfg, sizeof(drum_trigger_cfg_t));
    // apply_cfg_atomic(&newcfg);
    printf("OK\n");
    fflush(stdout);
}


int main() {

	// variables
	int i;
	uint16_t head;
	uint16_t rim;
	uint16_t result;
	drum_trigger_state_t st;
	drum_trigger_init(&st);
    char linebuf[512];
    int32_t signal;

	// 5kz sampling
	drum_trigger_cfg_t cfg = {
		.th_high_head = 250, .th_low_head = 120,
		.th_high_rim = 250,	.th_low_rim	= 120,

		.scan_min_ms = 2,		// proche “scan time” des modules
		.release_ms	 = 4,		// fin de frappe rapide -> bon pour roulements
		.max_group_ms = 30,		// sécurité

		.retrigger_head_ms = 18,
		.retrigger_rim_ms = 18,

		.both_ratio_q15 = (uint32_t)(1.50f * 32768.0f),		// si pics à moins de 50% -> BOTH
		.min_secondary_for_both = 300						// évite faux BOTH sur crosstalk faible
	};

	/*
	// 10kz sampling
	drum_trigger_cfg_t cfg = {
		.th_high_head = 250, .th_low_head = 120,
		.th_high_rim = 250,	.th_low_rim	= 120,

		.scan_min_ms = 2,		// proche “scan time” des modules
		.release_ms	 = 3,		// fin de frappe rapide -> bon pour roulements
		.max_group_ms = 30,		// sécurité

		.retrigger_head_ms = 15,
		.retrigger_rim_ms = 15,

		.both_ratio_q15 = (uint32_t)(1.50f * 32768.0f),		// si pics à moins de 50% -> BOTH
		.min_secondary_for_both = 300						// évite faux BOTH sur crosstalk faible
	};
	*/

	// Initialize the standard I/O
	stdio_init_all();

	// Select ADC0 and ADC1
	adc_init();
	adc_gpio_init(26); // GPIO 26 corresponds to ADC0
	adc_gpio_init(27); // GPIO 27 corresponds to ADC1
	adc_set_temp_sensor_enabled(false);

	while (1) {
		// read ADC
		adc_select_input(0);
		signal = (int32_t) (adc_read() - 2048); // idle adc_read = 2048; on recentre à 0
        rim = (uint16_t) abs (signal);

		adc_select_input(1);
		signal = (int32_t) (adc_read() - 2048); // idle adc_read = 2048; on recentre à 0
        head = (uint16_t) abs (signal);

		// determine if drum was hit
		drum_hit_t hit = drum_trigger_update (&st, &cfg, head, rim, to_ms_since_boot(get_absolute_time()));
        // envoie le hit au PC (ou affiche sur console) pour aider à choisir les seuils
        if (hit.kind != DRUM_HIT_NONE) {
            send_hit_csv(hit.t_ms, hit.peak_head, hit.peak_rim,
                         0, 0, // durées non mesurées dans cette version simple
                         (hit.kind == DRUM_HIT_HEAD) ? "HEAD" :
                         (hit.kind == DRUM_HIT_RIM) ? "RIM" :
                         (hit.kind == DRUM_HIT_BOTH) ? "BOTH" : "");
        }

        // vérifier si une ligne JSON est arrivée (timeout court)
        if (read_line_uart(linebuf, sizeof(linebuf), 10)) {
            // si la ligne commence par '{', traiter comme config
            if (linebuf[0] == '{') {
                handle_incoming_config(linebuf, &cfg);
            } else {
                // optionnel: autres commandes (ex: "PING", "GETCFG")
                if (strncmp(linebuf, "GETCFG", 6) == 0) {
                    // renvoyer config actuelle en JSON (utile au PC)
                    // printf("{\"th_high_head\":%d,...}\n", ...);
                }
            }
        }
        // faire d'autres tâches (détection piezo, etc.)
        tight_loop_contents();
	}
}
