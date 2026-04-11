#!/usr/bin/env python3
"""
calibrate_edrum.py

Iterative e-drum calibration over USB CDC.
Communicates with Pico firmware via JSON over USB serial.
Step-by-step: HEAD -> RIM -> BOTH -> Finalize.
"""

import sys
import time
import json
import serial
import argparse
from collections import defaultdict
import numpy as np

# Config
DEFAULT_SERIAL = '/dev/ttyACM0' if sys.platform != 'win32' else 'COM3'
BAUD = 115200  # USB CDC baud (ignored, but set)
OUTPUT_CFG_JSON = 'drum_cfg_calibrated.json'

# Helpers
def percentile_np(arr, p):
    return float(np.percentile(arr, p)) if len(arr) > 0 else None

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def send_json(ser, data):
    if hasattr(ser, 'reset_input_buffer'):
        ser.reset_input_buffer()
    msg = json.dumps(data) + '\n'
    ser.write(msg.encode('utf-8'))
    ser.flush()

def read_json_line(ser, timeout=1.0):
    start = time.time()
    buffer = ''
    while time.time() - start < timeout:
        if ser.in_waiting:
            c = ser.read(1).decode('utf-8', errors='ignore')
            if c == '\n':
                try:
                    return json.loads(buffer.strip())
                except json.JSONDecodeError:
                    pass  # Ignore invalid JSON
                buffer = ''
            else:
                buffer += c
        time.sleep(0.01)
    return None

def collect_hits(ser, count, timeout_per_hit=3.0, show_debug=False, filter_kind=None):
    hits = []
    all_hits = []
    start = time.time()
    last_print = time.time()
    while len(hits) < count and time.time() - start < timeout_per_hit * count:
        line = ser.readline().decode('utf-8', errors='ignore').strip()
        if line:
            try:
                data = json.loads(line)
                if 'hit' in data:
                    all_hits.append(data['hit'])
                    if filter_kind is None or data['hit'].get('kind') == filter_kind:
                        hits.append(data['hit'])
                        print(f"  Hit {len(hits)}: {data['hit']}")
                    else:
                        print(f"  [Skipped {data['hit']['kind']}] (expected {filter_kind})")
                elif 'debug' in data and show_debug:
                    dbg = data['debug']
                    print(f"  [DBG] head={dbg['adc_head']:4d} rim={dbg['adc_rim']:4d} group={dbg['group_active']} pk_h={dbg['peak_head']:4d} pk_r={dbg['peak_rim']:4d}")
            except json.JSONDecodeError:
                pass
        # Print progress every 2 seconds
        if time.time() - last_print > 2:
            elapsed = int(time.time() - start)
            hint = f" ({len(hits)}/{count} {filter_kind})" if filter_kind else ""
            print(f"  [Collecting{hint}... {elapsed}s elapsed, {len(all_hits)} total received]")
            last_print = time.time()
        time.sleep(0.01)
    return hits, all_hits

def estimate_retrigger_ms(hits, min_ms=15, max_ms=80, percentile=20):
    if len(hits) < 2:
        return 30
    times = np.array([h['t_ms'] for h in hits], dtype=float)
    diffs = np.diff(times)
    if len(diffs) == 0:
        return 30
    value = float(np.percentile(diffs, percentile))
    return int(clamp(value, min_ms, max_ms))


def estimate_release_ms(hits, min_ms=15, max_ms=60, percentile=10):
    if len(hits) < 2:
        return 30
    times = np.array(sorted(h['t_ms'] for h in hits), dtype=float)
    diffs = np.diff(times)
    if len(diffs) == 0:
        return 30
    value = float(np.percentile(diffs, percentile))
    return int(clamp(value, min_ms, max_ms))


def estimate_head_cfg(hits):
    if not hits:
        return {}
    peaks = [h['peak_head'] for h in hits if h['peak_head'] > 0]
    if not peaks:
        return {}
    arr = np.array(peaks, dtype=float)
    noise = percentile_np(arr, 5) or 0
    std = float(np.std(arr)) or 1
    max_peak = int(np.max(arr))
    min_peak = int(np.min(arr))
    th_low = int(clamp(noise + 3 * std, 1, min_peak - 1))
    th_low = clamp(th_low, 1, max_peak - 1)
    median = int(np.median(arr))
    weak_p = int(percentile_np(arr, 20) or median)
    th_high = int(clamp(max(th_low + 40, weak_p), 1, max_peak))
    retrigger = estimate_retrigger_ms(hits, min_ms=15, max_ms=70)
    return {
        'th_high_head': th_high,
        'th_low_head': th_low,
        'retrigger_head_ms': retrigger
    }

def estimate_rim_cfg(hits):
    if not hits:
        return {}
    peaks = [h['peak_rim'] for h in hits if h['peak_rim'] > 0]
    if not peaks:
        return {}
    arr = np.array(peaks, dtype=float)
    noise = percentile_np(arr, 5) or 0
    std = float(np.std(arr)) or 1
    max_peak = int(np.max(arr))
    min_peak = int(np.min(arr))
    max_safe_peak = int(np.percentile(arr[arr < 2048] if np.any(arr < 2048) else arr, 95))
    max_safe_peak = max(max_safe_peak, min_peak + 20)
    if max_peak >= 2048:
        max_safe_peak = min(max_safe_peak, 2047 - 20)
    max_safe_peak = min(max_safe_peak, max_peak)

    th_low = int(clamp(noise + 3 * std, 1, min_peak - 1))
    th_low = clamp(th_low, 1, max_safe_peak - 20)
    median = int(np.median(arr))
    weak_p = int(percentile_np(arr, 20) or median)
    th_high = int(clamp(max(th_low + 40, weak_p), 1, max_safe_peak))
    retrigger = estimate_retrigger_ms(hits, min_ms=15, max_ms=70)
    return {
        'th_high_rim': th_high,
        'th_low_rim': th_low,
        'retrigger_rim_ms': retrigger
    }

def wait_for_config_ack(ser, timeout=5):
    """Wait for config_updated ack from MCU with timeout."""
    start = time.time()
    while time.time() - start < timeout:
        line = ser.readline().decode('utf-8', errors='ignore').strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if data.get('status') == 'config_updated':
                return True
        except json.JSONDecodeError:
            pass
    print("  Warning: No config ack received (timeout)")
    return False

def estimate_both_cfg(hits, head_median, rim_median):
    if not hits:
        return {}
    ratios = []
    secondaries = []
    for h in hits:
        mx = max(h['peak_head'], h['peak_rim'])
        mn = min(h['peak_head'], h['peak_rim'])
        if mn > 0:
            ratios.append(mx / mn)
            secondaries.append(mn)
    if not ratios:
        return {}
    ratio = float(np.median(ratios))
    ratio = max(1.2, min(ratio * 1.15, 1.5))
    both_ratio_q15 = int(ratio * 32768)

    low_secondary = int(np.percentile(secondaries, 20) * 0.85)
    safe_floor = int(max(200,
                         0.25 * max(head_median, rim_median),
                         0.35 * min(head_median, rim_median)))
    min_sec = int(min(low_secondary, 0.75 * min(head_median, rim_median)))
    min_sec = int(max(min_sec, safe_floor))

    return {
        'both_ratio_q15': both_ratio_q15,
        'min_secondary_for_both': min_sec
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', '-p', default=DEFAULT_SERIAL)
    args = parser.parse_args()

    ser = serial.Serial(args.port, BAUD, timeout=1)
    time.sleep(1)  # Wait for connection

    # Wait for ready
    print("Waiting for MCU ready...")
    while True:
        line = ser.readline().decode('utf-8', errors='ignore').strip()
        if line:
            try:
                data = json.loads(line)
                if data.get('status') == 'ready':
                    print("MCU is ready!")
                    break
            except json.JSONDecodeError:
                pass

    cfg = {
        'th_high_head': 2000, 'th_low_head': 1800,
        'th_high_rim': 2000, 'th_low_rim': 1800,
        'scan_min_ms': 10, 'release_ms': 30, 'max_group_ms': 250,
        'retrigger_head_ms': 30, 'retrigger_rim_ms': 30,
        'both_ratio_q15': 32768, 'min_secondary_for_both': 500
    }

    print("\n=== PHASE 1: HEAD ===")
    input("Hit the HEAD several times (strong and weak). Press Enter to start collecting 20 hits.")
    hits_head, _ = collect_hits(ser, 20, filter_kind='HEAD')
    head_cfg = estimate_head_cfg(hits_head)
    cfg.update(head_cfg)
    send_json(ser, cfg)
    print("Sent updated config for HEAD.")
    wait_for_config_ack(ser)

    # Phase 2: RIM
    print("\n=== PHASE 2: RIM ===")
    input("Hit the RIM several times (strong and weak). Press Enter to start collecting 20 hits.")
    hits_rim, _ = collect_hits(ser, 20, filter_kind='RIM')
    rim_cfg = estimate_rim_cfg(hits_rim)
    cfg.update(rim_cfg)
    combined_hits = hits_head + hits_rim
    cfg['release_ms'] = estimate_release_ms(combined_hits, min_ms=15, max_ms=60)
    send_json(ser, cfg)
    print("Sent updated config for RIM.")
    print(f"  release_ms set to {cfg['release_ms']} based on combined hit timing.")
    wait_for_config_ack(ser)

    # Phase 3: BOTH
    print("\n=== PHASE 3: BOTH (Rimshots) ===")
    print("Relaxing thresholds to detect rimshots...")
    # Relax thresholds by 30% to make detection easier for rimshots
    relaxed_cfg = cfg.copy()
    relaxed_cfg['th_high_head'] = int(cfg['th_high_head'] * 0.7)
    relaxed_cfg['th_low_head'] = int(cfg['th_low_head'] * 0.7)
    relaxed_cfg['th_high_rim'] = int(cfg['th_high_rim'] * 0.7)
    relaxed_cfg['th_low_rim'] = int(cfg['th_low_rim'] * 0.7)
    send_json(ser, relaxed_cfg)
    print("Sent relaxed config for BOTH detection.")
    wait_for_config_ack(ser)
    
    input("Play rimshots (hit both head and rim simultaneously). Press Enter to start collecting 15 hits.")
    print("Collecting rimshots...")
    print("Note: Only hits classified as BOTH will be counted. Accidental single hits will be skipped.")
    hits_both, all_hits_both = collect_hits(ser, 15, show_debug=False, filter_kind='BOTH')
    
    if not hits_both:
        print("\nNo BOTH hits detected. Summary of all hits received:")
        both_breakdown = defaultdict(int)
        for h in all_hits_both:
            both_breakdown[h['kind']] += 1
        for kind, count in both_breakdown.items():
            print(f"  {kind}: {count} hits")
        print("\nTry again with firmer, more synchronized hits on both head and rim.")
        print("Skipping BOTH phase estimation. Using default BOTH config.")
    else:
        print(f"\nBOTH hits collected: {len(hits_both)}")
        if len(all_hits_both) > len(hits_both):
            print(f"Note: {len(all_hits_both) - len(hits_both)} accidental single hits were skipped")
            both_breakdown = defaultdict(int)
            for h in all_hits_both:
                both_breakdown[h['kind']] += 1
            for kind, count in both_breakdown.items():
                print(f"  Total {kind}: {count}")
        head_median = np.median([h['peak_head'] for h in hits_head if h['peak_head'] > 0]) if hits_head else 2000
        rim_median = np.median([h['peak_rim'] for h in hits_rim if h['peak_rim'] > 0]) if hits_rim else 2000
        both_cfg = estimate_both_cfg(hits_both, head_median, rim_median)
        cfg.update(both_cfg)
    
    # Send final config to MCU
    send_json(ser, cfg)
    print("Sent final config to MCU.")
    wait_for_config_ack(ser)

    # Finalize
    print("\n=== FINAL CONFIG ===")
    print(json.dumps(cfg, indent=2))
    with open(OUTPUT_CFG_JSON, 'w') as f:
        json.dump(cfg, f, indent=2)
    print(f"Saved to {OUTPUT_CFG_JSON}")

    # C struct
    c_block = [
        "drum_trigger_cfg_t cfg = {",
        f"    .th_high_head = {cfg['th_high_head']}, .th_low_head = {cfg['th_low_head']},",
        f"    .th_high_rim  = {cfg['th_high_rim']}, .th_low_rim  = {cfg['th_low_rim']},",
        "",
        f"    .scan_min_ms = {cfg['scan_min_ms']},",
        f"    .release_ms  = {cfg['release_ms']},",
        f"    .max_group_ms = {cfg['max_group_ms']},",
        "",
        f"    .retrigger_head_ms = {cfg['retrigger_head_ms']},",
        f"    .retrigger_rim_ms  = {cfg['retrigger_rim_ms']},",
        "",
        f"    .both_ratio_q15 = (uint32_t)({cfg['both_ratio_q15']}),",
        f"    .min_secondary_for_both = {cfg['min_secondary_for_both']}",
        "};"
    ]
    print("\nC struct to paste into main firmware:")
    print("\n".join(c_block))

    ser.close()
    print("Calibration complete.")

if __name__ == '__main__':
    main()
