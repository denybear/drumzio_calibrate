#!/usr/bin/env python3
import sys
import time
import json
import serial
import numpy as np

DEFAULT_SERIAL = 'COM3' if sys.platform == 'win32' else '/dev/ttyACM0'
BAUD = 115200 

def send_json(ser, data):
    ser.reset_input_buffer() # Nettoie avant d'envoyer
    msg = json.dumps(data) + '\n'
    ser.write(msg.encode('utf-8'))
    ser.flush()
    time.sleep(0.1)

def read_json_line(ser, timeout=0.1):
    ser.timeout = timeout
    line = ser.readline().decode('utf-8', errors='ignore').strip()
    try:
        return json.loads(line) if line else None
    except:
        return None

def flush_serial(ser):
    """Vide le tampon série pour ignorer les frappes passées."""
    ser.reset_input_buffer()
    while ser.in_waiting:
        ser.read(ser.in_waiting)

def collect_hits(ser, count, target_kind, instruction):
    print(f"\n[ACTION] : {instruction}")
    input(f"Prêt ? Appuyez sur Entrée pour collecter {count} frappes...")
    flush_serial(ser)
    hits = []
    while len(hits) < count:
        data = read_json_line(ser, timeout=1.0)
        if data and 'hit' in data:
            h = data['hit']
            if h['kind'] == target_kind:
                hits.append(h)
                val = h['p_h'] if target_kind == 'HEAD' else h['p_r']
                print(f"  {len(hits)}/{count} : {h['kind']} (Peak: {val})")
    return hits

def main():
    try:
        ser = serial.Serial(DEFAULT_SERIAL, BAUD, timeout=0.1)
    except:
        print("Erreur: Port série inaccessible."); return
    time.sleep(2)

    # Config de départ sécurisée
    cfg = {
        "th_high_head": 1500, "th_low_head": 1000,
        "th_high_rim": 1500, "th_low_rim": 1000,
        "scan_min_us": 2500, "retrigger_us": 35000, "crosstalk_min_us": 25000
    }
    send_json(ser, cfg)

    # --- PHASE 1 : HEAD (Ghost -> Power -> Validation) ---
    print("\n=== CALIBRATION DU HEAD (Peau) ===")
    ghosts = collect_hits(ser, 20, "HEAD", "Tapez 20 coups TRÈS DOUX (Ghost Notes).")
    cfg['th_high_head'] = int(np.min([h['p_h'] for h in ghosts]) * 0.5)
    cfg['th_low_head'] = int(cfg['th_high_head'] * 0.8)
    send_json(ser, cfg)

    powers = collect_hits(ser, 20, "HEAD", "Tapez 20 coups TRÈS FORTS (Power Hits).")
    intervals = np.diff([h['t_us'] for h in powers])
    rebonds = intervals[intervals < 120000]
    if len(rebonds) > 0:
        cfg['retrigger_us'] = int(np.max(rebonds) + 10000)
    send_json(ser, cfg)

    collect_hits(ser, 10, "HEAD", "VALIDATION : Tapez 10 coups variés. Vérifiez l'absence de doubles frappes.")

    # --- PHASE 2 : RIM (Ghost -> Power -> Validation) ---
    print("\n=== CALIBRATION DU RIM (Cercle) ===")
    ghosts_r = collect_hits(ser, 20, "RIM", "Tapez 20 coups TRÈS DOUX sur le Rim.")
    cfg['th_high_rim'] = int(np.min([h['p_r'] for h in ghosts_r]) * 0.5)
    cfg['th_low_rim'] = int(cfg['th_high_rim'] * 0.8)
    send_json(ser, cfg)

    powers_r = collect_hits(ser, 20, "RIM", "Tapez 20 coups TRÈS FORTS sur le Rim.")
    intervals_r = np.diff([h['t_us'] for h in powers_r])
    rebonds_r = intervals_r[intervals_r < 120000]
    if len(rebonds_r) > 0:
        cfg['retrigger_us'] = max(cfg['retrigger_us'], int(np.max(rebonds_r) + 10000))
    send_json(ser, cfg)

    collect_hits(ser, 10, "RIM", "VALIDATION : Tapez 10 coups variés sur le Rim.")

    # --- PHASE 3 : VITESSE (Doubles/Triples) ---
    print("\n=== TEST DE VITESSE (Doubles/Triples) ===")
    fast_h = collect_hits(ser, 15, "HEAD", "Tapez des séries de DOUBLES/TRIPLES rapides sur le HEAD.")
    fast_r = collect_hits(ser, 15, "RIM", "Tapez des séries de DOUBLES/TRIPLES rapides sur le RIM.")
    
    # On ajuste le retrigger si tu es plus rapide que le filtre actuel
    all_fast = fast_h + fast_r
    if len(all_fast) > 5:
        intv = np.diff(sorted([h['t_us'] for h in all_fast]))
        real_fast = intv[(intv > 15000) & (intv < 80000)]
        if len(real_fast) > 0:
            suggested = int(np.min(real_fast) * 0.85)
            if suggested < cfg['retrigger_us']:
                print(f"  Note: Vitesse détectée ({suggested}us). Retrigger abaissé pour la performance.")
                cfg['retrigger_us'] = suggested
    send_json(ser, cfg)

    # --- PHASE 4 : CROSSTALK (RIM vers HEAD) ---
    print("\n=== VÉRIFICATION CROSSTALK (Priorité RIM) ===")
    print("[ACTION] : Tapez 10 coups FORTS sur le RIM.")
    input("Appuyez sur Entrée pour commencer l'observation...")
    flush_serial(ser)
    
    rim_count = 0
    while rim_count < 10:
        data = read_json_line(ser, timeout=2.0)
        if data and 'hit' in data:
            h = data['hit']
            if h['kind'] == "RIM":
                rim_count += 1
                print(f"  Coup Rim {rim_count}/10 enregistré.")
            elif h['kind'] == "HEAD":
                print(f"  (!) Erreur: Le Head s'est déclenché ! (Peak Head: {h['p_h']})")
                if h['p_h'] >= cfg['th_high_head']:
                    cfg['th_high_head'] = h['p_h'] + 150
                    cfg['th_low_head'] = int(cfg['th_high_head'] * 0.8)
                    print(f"  >> Seuil Head remonté à {cfg['th_high_head']} pour éviter ce crosstalk.")
                    send_json(ser, cfg)

    # --- EXPORT FINAL ---
    print("\n" + "="*50)
    print("CALIBRATION TERMINÉE")
    print("="*50)
    print(f"""
static const drum_trigger_cfg_t trigger_cfg = {{
    .th_high_head = {cfg['th_high_head']}, .th_low_head = {cfg['th_low_head']},
    .th_high_rim  = {cfg['th_high_rim']}, .th_low_rim  = {cfg['th_low_rim']},
    .scan_min_us  = {cfg['scan_min_us']},
    .retrigger_us = {cfg['retrigger_us']},
    .crosstalk_min_us = {cfg['crosstalk_min_us']}
}};""")
    ser.close()

if __name__ == "__main__":
    main()