#!/usr/bin/env python3
"""
calibrate_edrum_numpy.py

Calibration helper for e-drum using pyserial + numpy.
MCU -> PC CSV expected per hit:
  t_us,peak_head,peak_rim,dur_head_ms,dur_rim_ms[,label]

PC -> MCU: JSON config (one-line, newline-terminated). MCU replies "OK" or "ERR <msg>".

Requires:
  pip install pyserial numpy
"""

import sys
import os
import time
import json
import math
import argparse
import statistics
from collections import defaultdict

import numpy as np
import serial

# === User config ===
DEFAULT_SERIAL = '/dev/ttyACM0' if os.name != 'nt' else 'COM3'
BAUD = 230400
READ_TIMEOUT = 0.1  # s
OUTPUT_CFG_JSON = 'drum_cfg_auto.json'

PLAN_COUNTS = {
    'silence_sec': 4,
    'head_strong': 30,
    'head_weak': 12,
    'head_doubles': 10,
    'head_triples': 8,
    'rim_strong': 30,
    'rim_weak': 12,
    'rim_doubles': 10,
    'rim_triples': 8,
    'rimshots': 20,
    'mixed_test': 20
}

NOISE_STD_FACTOR = 4.0
MIN_TH_GAP = 60
MIN_SECONDARY_FRACTION = 0.25
BOTH_RATIO_FALLBACK = 1.4

# === Utilities ===

def percentile_np(arr, p):
    return float(np.percentile(arr, p)) if len(arr) > 0 else None

def q15_from_float(x):
    return int(round(x * 32768.0))

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def parse_csv_line(line):
    # expects t_us,peak_head,peak_rim,dur_head_ms,dur_rim_ms[,label]
    parts = [p.strip() for p in line.split(',') if p.strip()!='']
    if len(parts) < 5:
        return None
    try:
        t_us = int(float(parts[0]))
        ph = int(float(parts[1]))
        pr = int(float(parts[2]))
        dh = float(parts[3])
        dr = float(parts[4])
        label = parts[5] if len(parts) >= 6 else ''
        return {'t_us': t_us, 'peak_head': ph, 'peak_rim': pr, 'dur_head': float(dh), 'dur_rim': float(dr), 'label': label}
    except Exception:
        return None

# === Serial IO wrapper ===

class SerialReader:
    def __init__(self, port, baud=230400, timeout=0.1):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.ser = None

    def open(self):
        self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        time.sleep(0.05)
        self.ser.reset_input_buffer()

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
            self.ser = None

    def readline(self):
        raw = self.ser.readline()
        if not raw:
            return None
        try:
            return raw.decode('utf-8', errors='replace').strip()
        except:
            return None

    def send_line(self, s):
        data = (s + '\n').encode('utf-8')
        self.ser.write(data)
        self.ser.flush()

# === Collection helpers ===

def collect_n_events(sr, target_n, timeout_per_event=6.0):
    evs = []
    last_received = time.time()
    while len(evs) < target_n:
        line = sr.readline()
        if line:
            parsed = parse_csv_line(line)
            if parsed:
                evs.append(parsed)
                print(f"  #{len(evs)} t_us={parsed['t_us']} ph={parsed['peak_head']} pr={parsed['peak_rim']}")
                last_received = time.time()
            else:
                # ignore unparsable
                pass
        else:
            if time.time() - last_received > timeout_per_event:
                print(f"  timeout after {timeout_per_event}s, collected {len(evs)} / {target_n}")
                break
    return evs

def plan_steps():
    p = PLAN_COUNTS
    return [
        ('silence', f"Remain silent for {p['silence_sec']} seconds (no hits).", p['silence_sec']),
        ('head_strong', "HEAD — strong single hits (~1s apart).", p['head_strong']),
        ('head_weak', "HEAD — weak/soft hits.", p['head_weak']),
        ('head_doubles', "HEAD — doubles / short rolls (2 hits).", p['head_doubles']),
        ('head_triples', "HEAD — triples / short rolls (3 hits).", p['head_triples']),
        ('rim_strong', "RIM — strong single hits.", p['rim_strong']),
        ('rim_weak', "RIM — weak/soft hits.", p['rim_weak']),
        ('rim_doubles', "RIM — doubles / short rolls (2 hits).", p['rim_doubles']),
        ('rim_triples', "RIM — triples / short rolls (3 hits).", p['rim_triples']),
        ('rimshots', "RIMSHOT — intentionally play rimshots (rim+head).", p['rimshots']),
        ('mixed_test', "Mixed quick play: alternate head/rim.", p['mixed_test'])
    ]

# === Estimation logic using numpy & clustering ===

def estimate_cfg_numpy(events):
    if not events:
        raise ValueError("No events to estimate from.")

    arr_ph = np.array([e['peak_head'] for e in events], dtype=np.float64)
    arr_pr = np.array([e['peak_rim'] for e in events], dtype=np.float64)
    arr_tus = np.array([e['t_us'] for e in events], dtype=np.int64)

    # Noise estimates: use lower 5th percentile
    noise_ph = percentile_np(arr_ph, 5)
    noise_pr = percentile_np(arr_pr, 5)
    ph_std = float(np.std(arr_ph)) if arr_ph.size>0 else 1.0
    pr_std = float(np.std(arr_pr)) if arr_pr.size>0 else 1.0

    th_low_head = int(clamp(noise_ph + NOISE_STD_FACTOR * ph_std, 1, 4095))
    th_low_rim  = int(clamp(noise_pr + NOISE_STD_FACTOR * pr_std, 1, 4095))

    # candidate peaks: those comfortably above low
    head_peaks = arr_ph[arr_ph > (th_low_head + 10)]
    rim_peaks  = arr_pr[arr_pr > (th_low_rim + 10)]
    if head_peaks.size == 0:
        head_peaks = arr_ph[np.argsort(arr_ph)[-max(1, min(5, len(arr_ph))):]]
    if rim_peaks.size == 0:
        rim_peaks = arr_pr[np.argsort(arr_pr)[-max(1, min(5, len(arr_pr))):]]

    median_head = int(np.median(head_peaks))
    median_rim  = int(np.median(rim_peaks))
    weak_head_p = int(percentile_np(head_peaks, 20))
    weak_rim_p  = int(percentile_np(rim_peaks, 20))

    th_high_head = int(clamp(max(th_low_head + MIN_TH_GAP, weak_head_p), 1, 4095))
    th_high_rim  = int(clamp(max(th_low_rim + MIN_TH_GAP, weak_rim_p), 1, 4095))

    # Durations: collect dur from events where peaks exceed high thresholds
    decay_head = np.array([e['dur_head'] for e in events if e['peak_head'] >= th_high_head and e['dur_head']>0], dtype=np.float64)
    decay_rim  = np.array([e['dur_rim']  for e in events if e['peak_rim']  >= th_high_rim and e['dur_rim']>0], dtype=np.float64)

    release_h = int(clamp(percentile_np(decay_head, 90) if decay_head.size>0 else 30, 5, 200))
    release_r = int(clamp(percentile_np(decay_rim, 90)  if decay_rim.size>0 else 30, 5, 200))
    release_ms = max(release_h, release_r)

    scan_min_ms = int(clamp(min(12, max(5, int(min(release_ms//3, 12)))), 5, 40))
    max_group_ms = int(clamp(max(250, 3*release_ms), 200, 2000))

    # IOI computation per zone (microsecond -> ms)
    evs_sorted = sorted(events, key=lambda e: e['t_us'])
    iois_head = []
    iois_rim = []
    last_h = None
    last_r = None
    for e in evs_sorted:
        if e['peak_head'] >= th_low_head:
            if last_h is not None:
                iois_head.append((e['t_us'] - last_h)/1000.0)
            last_h = e['t_us']
        if e['peak_rim'] >= th_low_rim:
            if last_r is not None:
                iois_rim.append((e['t_us'] - last_r)/1000.0)
            last_r = e['t_us']

    if iois_head:
        retrigger_head_ms = int(clamp(max(5, np.percentile(iois_head, 10) - 5), 5, 200))
    else:
        retrigger_head_ms = 30
    if iois_rim:
        retrigger_rim_ms = int(clamp(max(5, np.percentile(iois_rim, 10) - 5), 5, 200))
    else:
        retrigger_rim_ms = 30

    # Clustering for BOTH vs dominant:
    # Build feature vectors: [ph_norm, pr_norm, dt_ratio optional]
    # Normalize by medians to reduce scale effects
    ph_n = arr_ph / max(1.0, np.median(arr_ph))
    pr_n = arr_pr / max(1.0, np.median(arr_pr))
    X = np.column_stack([ph_n, pr_n])

    # k-means k=2 simple implementation (numpy)
    def kmeans2(X, k=2, niter=50):
        rng = np.random.default_rng(12345)
        idx = rng.choice(len(X), size=k, replace=False)
        centers = X[idx]
        for _ in range(niter):
            d = np.sum((X[:, None, :] - centers[None, :, :])**2, axis=2)
            labels = np.argmin(d, axis=1)
            new_centers = np.array([X[labels==i].mean(axis=0) if np.any(labels==i) else centers[i] for i in range(k)])
            if np.allclose(new_centers, centers):
                break
            centers = new_centers
        return labels, centers

    labels, centers = kmeans2(X, k=2)
    # Determine which cluster corresponds to BOTH: cluster center with both coords > ~0.6
    # Compute cluster properties
    cl_props = []
    for c in range(2):
        members = X[labels==c]
        if members.size == 0:
            cl_props.append({'size':0, 'center':centers[c], 'frac_both':0.0})
            continue
        # bothness fraction: proportion where both normalized peaks > 0.4
        frac_both = float(np.mean((members[:,0] > 0.4) & (members[:,1] > 0.4)))
        cl_props.append({'size':len(members), 'center':centers[c], 'frac_both':frac_both})

    # Choose both-cluster as one with higher frac_both
    both_cluster = 0 if cl_props[0]['frac_both'] >= cl_props[1]['frac_both'] else 1
    both_indices = np.where(labels==both_cluster)[0]
    both_events = [events[i] for i in both_indices]

    ratios = []
    secondaries = []
    for e in both_events:
        mx = max(e['peak_head'], e['peak_rim'])
        mn = min(e['peak_head'], e['peak_rim'])
        if mn > 0:
            ratios.append(mx / mn)
            secondaries.append(mn)

    if ratios:
        both_ratio = float(np.median(ratios))  # median ratio among intentional boths
        # set threshold slightly above median to be inclusive
        both_ratio_thr = max(1.05, both_ratio * 1.15)
    else:
        both_ratio_thr = BOTH_RATIO_FALLBACK

    both_ratio_q15 = q15_from_float(both_ratio_thr)

    if secondaries:
        min_secondary_for_both = int(max(np.percentile(secondaries, 30), MIN_SECONDARY_FRACTION * min(median_head, median_rim)))
    else:
        min_secondary_for_both = int(max(200, MIN_SECONDARY_FRACTION * min(median_head, median_rim)))

    cfg = {
        'th_high_head': int(th_high_head),
        'th_low_head' : int(th_low_head),
        'th_high_rim' : int(th_high_rim),
        'th_low_rim'  : int(th_low_rim),
        'scan_min_ms' : int(scan_min_ms),
        'release_ms'  : int(release_ms),
        'max_group_ms': int(max_group_ms),
        'retrigger_head_ms': int(retrigger_head_ms),
        'retrigger_rim_ms' : int(retrigger_rim_ms),
        'both_ratio_q15'   : int(both_ratio_q15),
        'min_secondary_for_both': int(min_secondary_for_both)
    }
    # diagnostics
    diag = {
        'median_head': median_head,
        'median_rim': median_rim,
        'both_ratio_est': both_ratio_thr,
        'both_cluster_size': int(len(both_events)),
        'ioi_head_ms_sample': iois_head[:5],
        'ioi_rim_ms_sample': iois_rim[:5]
    }
    return cfg, diag

# === Validation ===

def validate_cfg(cfg):
    errs = []
    for k in ['th_high_head','th_low_head','th_high_rim','th_low_rim']:
        v = cfg.get(k,0)
        if not (0 < v <= 4095):
            errs.append(f"{k}={v} out of 1..4095")
    if cfg['th_low_head'] >= cfg['th_high_head']:
        errs.append("th_low_head >= th_high_head")
    if cfg['th_low_rim'] >= cfg['th_high_rim']:
        errs.append("th_low_rim >= th_high_rim")
    if cfg['scan_min_ms'] < 1 or cfg['scan_min_ms'] > cfg['release_ms']:
        errs.append("scan_min_ms invalid")
    if cfg['release_ms'] < 1 or cfg['release_ms'] > 2000:
        errs.append("release_ms invalid")
    if cfg['retrigger_head_ms'] < 1 or cfg['retrigger_rim_ms'] < 1:
        errs.append("retrigger times invalid")
    if cfg['both_ratio_q15'] == 0:
        errs.append("both_ratio_q15 == 0")
    return errs

# === Interactive flow ===

def interactive_session(serial_port):
    sr = SerialReader(serial_port, BAUD, READ_TIMEOUT)
    sr.open()
    try:
        events = []
        steps = plan_steps()
        for key, prompt, count in steps:
            print("\n== STEP:", key)
            if key == 'silence':
                print(prompt)
                time.sleep(PLAN_COUNTS['silence_sec'])
                continue
            input(f"{prompt}\nPress Enter to start collecting {count} events.")
            collected = collect_n_events(sr, count)
            # label and append
            for e in collected:
                e['label'] = key
                events.append(e)
            print(f"Collected {len(collected)} events for {key}")
        print("\nEstimation in progress...")
        cfg, diag = estimate_cfg_numpy(events)
        print("Suggested config:")
        print(json.dumps(cfg, indent=2))
        print("Diagnostics:", diag)
        errs = validate_cfg(cfg)
        if errs:
            print("Validation errors:", errs)
            return None

        # send and wait ack
        payload = json.dumps(cfg)
        sr.send_line(payload)
        # wait for reply
        start = time.time()
        reply = None
        while time.time() - start < 5.0:
            line = sr.readline()
            if line:
                line = line.strip()
                if line:
                    reply = line
                    break
            time.sleep(0.01)
        if not reply:
            print("No reply from MCU after sending config.")
            # offer to save locally
            ans = input("Save config locally and retry? (y/n): ")
            if ans.lower().startswith('y'):
                with open(OUTPUT_CFG_JSON, 'w') as f:
                    json.dump(cfg, f, indent=2)
                print("Saved to", OUTPUT_CFG_JSON)
                return cfg
            else:
                return None
        print("MCU replied:", reply)
        if reply.startswith("OK"):
            # ask user to test
            ans = input("Apply and test? Are you satisfied? (y to accept, n to recalibrate): ")
            if ans.lower().startswith('y'):
                with open(OUTPUT_CFG_JSON, 'w') as f:
                    json.dump(cfg, f, indent=2)
                print("Final config saved to", OUTPUT_CFG_JSON)
                print(json.dumps(cfg, indent=2))
                return cfg
            else:
                print("User requested recalibration.")
                return None
        else:
            print("MCU returned error:", reply)
            return None

    finally:
        sr.close()

# === CLI ===

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--port', '-p', default=DEFAULT_SERIAL, help='serial port (e.g. /dev/ttyACM0)')
    args = p.parse_args()

    while True:
        res = interactive_session(args.port)
        if res:
            print("Calibration complete.")
            break
        ans = input("Retry entire calibration? (y/n): ")
        if not ans.lower().startswith('y'):
            print("Exiting without final config.")
            break

if __name__ == '__main__':
    main()
