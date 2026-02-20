#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
IFR 2023A Frequency Standard Calibration via Alpha250
======================================================

Calibrates the internal TCXO of the IFR 2023A synthesizer by adjusting
its Coarse DAC (CDAC) and Fine DAC (FDAC) values, using the Alpha250
delta-f measurement as a frequency reference.

Measurement is done at 62.5 MHz (fs/4) where delta_f = 0 Hz when the synth
is exactly on frequency. Any measured offset is the TCXO error.

Cal mode (DIAG:CAL:FST) does NOT change the RF output frequency — the
carrier stays at whatever was set. DAC adjustments take effect in real time,
so we stay in cal mode throughout the tuning phases and measure continuously.

Requires Level 2 unlock on the IFR 2023A: UTIL 80, password 123456.

Usage:
    python metrology/calibrate_synth.py
    python metrology/calibrate_synth.py --measure-only
    python metrology/calibrate_synth.py --dry-run
    python metrology/calibrate_synth.py --auto-save
    python metrology/calibrate_synth.py --target-ppb 50
"""

import sys
import os
import json
import time
import argparse
import numpy as np
from datetime import datetime, timezone

# Add parent directory for fpga_driver import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from metrology_common import DEMOD_FREQUENCY_HZ, DEFAULT_AMPLITUDE_DBM, format_deltaf


# ── Constants ────────────────────────────────────────────────────────

MEASURE_FREQ_HZ = DEMOD_FREQUENCY_HZ   # 62.5 MHz — delta_f = 0 Hz when on frequency
AMPLITUDE_DBM = 3.0
DEFAULT_TARGET_PPB = 100                # default convergence target
DAC_SETTLE_S = 2                        # settling after DAC change (TCXO tuning)
DISCARD_FIRST_SAMPLES = 2              # transient guard


# ── Connection helpers (from metrology_measure.py) ───────────────────

MAX_RECONNECT_ATTEMPTS = 3
RECONNECT_DELAY_S = 5


def connect_fpga(host, port):
    """Connect to Alpha250 with retry logic."""
    from fpga_driver import FPGADriver
    for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
        try:
            fpga = FPGADriver(host=host, auto_connect=True, port=port)
            if fpga.is_connected():
                return fpga
        except Exception as e:
            print(f"  FPGA connection attempt {attempt}/{MAX_RECONNECT_ATTEMPTS} failed: {e}")
            if attempt < MAX_RECONNECT_ATTEMPTS:
                time.sleep(RECONNECT_DELAY_S)
    raise ConnectionError(f"Failed to connect to FPGA at {host}:{port}")


def connect_synth(port):
    """Connect to IFR2023A with retry logic."""
    sys.path.insert(0, '/home/manip/src/ifr2023')
    from ifr2023a import IFR2023A
    for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
        try:
            synth = IFR2023A(port=port)
            idn = synth.idn()
            print(f"  Synth IDN: {idn}")
            return synth, idn
        except Exception as e:
            print(f"  Synth connection attempt {attempt}/{MAX_RECONNECT_ATTEMPTS} failed: {e}")
            if attempt < MAX_RECONNECT_ATTEMPTS:
                time.sleep(RECONNECT_DELAY_S)
    raise ConnectionError(f"Failed to connect to synthesizer on {port}")


# ── Measurement helpers ──────────────────────────────────────────────

def disable_servo_ch1(fpga):
    """Disable all servo loops on channel 1 (open-loop measurement)."""
    fpga.set_p_enable_1(False)
    fpga.set_first_integrator_on_1(False)
    fpga.set_second_integrator_on_1(False)
    fpga.set_third_integrator_on_1(False)
    fpga.set_phase_advance_enable_1(False)


def flush_history(fpga):
    """Drain the delta-f history buffer and return last sequence counter."""
    last_seq = 0
    for _ in range(5):
        samples = fpga.get_deltaf_history_since(last_seq)
        if not samples:
            break
        last_seq = samples[-1]['seq_counter']
        time.sleep(0.2)
    return last_seq


def measure_offset(fpga, duration_s, label=""):
    """Measure the mean delta-f offset on channel 1 (PI mode).

    Args:
        fpga: Connected FPGADriver
        duration_s: Number of 1-second samples to collect
        label: Optional label for progress display

    Returns:
        (mean_hz, std_hz, n_samples) or raises if insufficient data
    """
    prefix = f"  [{label}] " if label else "  "
    last_seq = flush_history(fpga)

    values = []
    raw_count = 0
    t0 = time.time()

    while time.time() - t0 < duration_s + DISCARD_FIRST_SAMPLES + 5:
        new = fpga.get_deltaf_history_since(last_seq)
        for s in new:
            raw_count += 1
            last_seq = s['seq_counter']
            if raw_count <= DISCARD_FIRST_SAMPLES:
                continue
            values.append(s['avg1_hz'])

        n = len(values)
        print(f"\r{prefix}Measuring {n}/{duration_s}s...", end='', flush=True)
        if n >= duration_s:
            break
        time.sleep(0.2)
    print()

    if len(values) < 3:
        raise RuntimeError(f"Insufficient samples: got {len(values)}, need at least 3")

    arr = np.array(values)
    return float(np.mean(arr)), float(np.std(arr)), len(values)


def set_dac_and_settle(synth, cdac=None, fdac=None):
    """Write DAC value(s) and wait for TCXO settling. Must be in cal mode."""
    if cdac is not None:
        synth.cal_fst_set_coarse_dac(cdac)
    if fdac is not None:
        synth.cal_fst_set_fine_dac(fdac)
    time.sleep(DAC_SETTLE_S)


def hz_to_ppb(offset_hz, carrier_hz=MEASURE_FREQ_HZ):
    """Convert frequency offset to ppb."""
    return offset_hz / carrier_hz * 1e9


def ppb_to_hz(ppb, carrier_hz=MEASURE_FREQ_HZ):
    """Convert ppb to Hz offset."""
    return ppb * carrier_hz / 1e9


# ── Data file I/O ────────────────────────────────────────────────────

def generate_data_path():
    """Generate timestamped calibration data file path."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(os.path.dirname(__file__), 'data', f'calibration_{ts}.json')


def save_data(data, filepath):
    """Atomic save: write to .tmp then rename."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    tmp = filepath + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, filepath)


# ── Calibration phases ───────────────────────────────────────────────

def phase0_verify(fpga, synth, data):
    """Phase 0: Initial verification and baseline measurement (before cal mode)."""
    print(f"\n{'='*70}")
    print("PHASE 0 — Initial verification")
    print(f"{'='*70}")

    phase = {'name': 'phase0_verify'}

    # Check Level 2 access
    print("  Checking Level 2 access (DIAG:CAL:FST:DATE?)...")
    try:
        cal_date = synth.cal_fst_get_date()
        print(f"    Last calibration date: {cal_date}")
        phase['last_cal_date'] = cal_date
    except Exception as e:
        print(f"\n  ERROR: Cannot access calibration commands: {e}")
        print("  → Unlock Level 2 on the front panel: UTIL 80, password 123456")
        raise RuntimeError("Level 2 access required. Use UTIL 80, password 123456.")

    # EXT10DIR reference check
    print("\n  Reference check: EXT10DIR mode, 10s measurement...")
    synth.set_clock_external_10mhz_direct()
    time.sleep(2.0)
    mean_ext, std_ext, n_ext = measure_offset(fpga, 10, "EXT ref check")
    print(f"    EXT10DIR: {format_deltaf(mean_ext)} ± {format_deltaf(std_ext)} (n={n_ext})")
    phase['ext_check'] = {'mean_hz': mean_ext, 'std_hz': std_ext, 'n': n_ext}

    if abs(mean_ext) > 1.0:
        print(f"  WARNING: EXT10DIR offset = {format_deltaf(mean_ext)}, expected ~0 Hz")
        print("           Check 10 MHz reference cable connection")

    # INT mode baseline
    print("\n  Baseline measurement: INT mode, 30s...")
    synth.set_clock_internal()
    time.sleep(2.0)
    mean_int, std_int, n_int = measure_offset(fpga, 30, "INT baseline")
    ppb_initial = hz_to_ppb(mean_int)
    print(f"    INT offset: {format_deltaf(mean_int)} = {ppb_initial:+.1f} ppb (n={n_int})")
    phase['int_baseline'] = {'mean_hz': mean_int, 'std_hz': std_int, 'n': n_int, 'ppb': ppb_initial}

    data['phases'].append(phase)
    return mean_int, ppb_initial


def phase1_cdac_sensitivity(fpga, synth, data, cdac, fdac):
    """Phase 1: Characterize CDAC sensitivity (Hz per step). Must be in cal mode."""
    print(f"\n{'='*70}")
    print("PHASE 1 — CDAC sensitivity characterization")
    print(f"{'='*70}")

    phase = {'name': 'phase1_cdac_sensitivity'}

    # Set FDAC to midrange for sensitivity measurement
    if fdac != 128:
        print(f"  Setting FDAC=128 (was {fdac}) for sensitivity measurement...")
        set_dac_and_settle(synth, fdac=128)

    mean_a, std_a, _ = measure_offset(fpga, 15, f"CDAC={cdac}")
    phase['point_a'] = {'cdac': cdac, 'fdac': 128, 'mean_hz': mean_a, 'std_hz': std_a}
    print(f"    CDAC={cdac}, FDAC=128: {format_deltaf(mean_a)}")

    # Measure at CDAC+10
    cdac_b = min(cdac + 10, 255)
    delta_cdac = cdac_b - cdac
    if delta_cdac == 0:
        cdac_b = max(cdac - 10, 0)
        delta_cdac = cdac_b - cdac

    set_dac_and_settle(synth, cdac=cdac_b)
    mean_b, std_b, _ = measure_offset(fpga, 15, f"CDAC={cdac_b}")
    phase['point_b'] = {'cdac': cdac_b, 'fdac': 128, 'mean_hz': mean_b, 'std_hz': std_b}
    print(f"    CDAC={cdac_b}, FDAC=128: {format_deltaf(mean_b)}")

    hz_per_cdac = (mean_b - mean_a) / delta_cdac
    phase['hz_per_cdac_step'] = hz_per_cdac
    print(f"    Sensitivity: {hz_per_cdac:+.3f} Hz/CDAC step ({hz_to_ppb(hz_per_cdac):+.1f} ppb/step)")

    # Restore original CDAC
    set_dac_and_settle(synth, cdac=cdac)

    data['phases'].append(phase)
    return hz_per_cdac, mean_a  # mean_a is the offset at (cdac, 128)


def phase2_coarse_correction(fpga, synth, data, offset_hz, hz_per_cdac, cdac, coarse_threshold_hz):
    """Phase 2: Coarse DAC correction (skip if already within threshold). Must be in cal mode."""
    print(f"\n{'='*70}")
    print("PHASE 2 — Coarse DAC correction")
    print(f"{'='*70}")

    phase = {'name': 'phase2_coarse', 'iterations': []}

    if abs(offset_hz) < coarse_threshold_hz:
        print(f"  Offset {format_deltaf(offset_hz)} already within coarse threshold "
              f"({format_deltaf(coarse_threshold_hz)}), skipping.")
        phase['skipped'] = True
        data['phases'].append(phase)
        return cdac, offset_hz

    fdac = 128  # FDAC stays at midrange during coarse phase

    for iteration in range(3):
        correction = round(-offset_hz / hz_per_cdac)
        new_cdac = int(np.clip(cdac + correction, 0, 255))
        print(f"\n  Iteration {iteration + 1}: offset={format_deltaf(offset_hz)}, "
              f"correction={correction:+d} → CDAC={new_cdac}")

        set_dac_and_settle(synth, cdac=new_cdac)
        mean_hz, std_hz, _ = measure_offset(fpga, 15, f"CDAC={new_cdac}")
        print(f"    Result: {format_deltaf(mean_hz)} ± {format_deltaf(std_hz)}")

        phase['iterations'].append({
            'cdac': new_cdac, 'offset_hz': mean_hz, 'std_hz': std_hz,
            'correction': correction,
        })

        cdac = new_cdac
        offset_hz = mean_hz

        if abs(offset_hz) < coarse_threshold_hz:
            print(f"  Coarse correction converged: {format_deltaf(offset_hz)}")
            break

    phase['final_cdac'] = cdac
    phase['final_offset_hz'] = offset_hz
    data['phases'].append(phase)
    return cdac, offset_hz


def phase3_fine_correction(fpga, synth, data, offset_hz, cdac, fdac, target_hz, hz_per_cdac):
    """Phase 3: Fine DAC correction with CDAC readjustment on saturation.

    Uses 30s measurements for sub-ppb precision (1 FDAC LSB ≈ 0.44 ppb).
    If FDAC hits a limit (0 or 255), adjusts CDAC by ±1 step and retries.
    """
    print(f"\n{'='*70}")
    print("PHASE 3 — Fine DAC correction")
    print(f"{'='*70}")

    FINE_MEAS_S = 30  # longer measurements for sub-ppb precision
    MAX_CDAC_READJUST = 2  # max CDAC readjustments for FDAC saturation

    phase = {'name': 'phase3_fine', 'iterations': [], 'cdac_readjustments': []}

    # Characterize FDAC sensitivity
    print("  Characterizing FDAC sensitivity...")
    mean_a, std_a, _ = measure_offset(fpga, FINE_MEAS_S, f"FDAC={fdac}")
    print(f"    FDAC={fdac}: {format_deltaf(mean_a)}")

    fdac_b = min(fdac + 20, 255)
    delta_fdac = fdac_b - fdac
    if delta_fdac == 0:
        fdac_b = max(fdac - 20, 0)
        delta_fdac = fdac_b - fdac

    set_dac_and_settle(synth, fdac=fdac_b)
    mean_b, std_b, _ = measure_offset(fpga, FINE_MEAS_S, f"FDAC={fdac_b}")
    print(f"    FDAC={fdac_b}: {format_deltaf(mean_b)}")

    hz_per_fdac = (mean_b - mean_a) / delta_fdac
    phase['hz_per_fdac_step'] = hz_per_fdac
    print(f"    Sensitivity: {hz_per_fdac:+.6f} Hz/FDAC step ({hz_to_ppb(hz_per_fdac):+.3f} ppb/step)")

    if abs(hz_per_fdac) < 1e-6:
        print("  WARNING: FDAC sensitivity too low, cannot calibrate fine DAC")
        data['phases'].append(phase)
        return cdac, fdac, mean_a

    # Restore FDAC
    set_dac_and_settle(synth, fdac=fdac)
    offset_hz = mean_a
    cdac_readjust_count = 0

    for iteration in range(8):  # extra iterations to account for CDAC readjustments
        correction = round(-offset_hz / hz_per_fdac)
        unclamped_fdac = fdac + correction
        new_fdac = int(np.clip(unclamped_fdac, 0, 255))

        # Check if FDAC is being clamped (saturated)
        if new_fdac != unclamped_fdac and cdac_readjust_count < MAX_CDAC_READJUST:
            # FDAC range insufficient — readjust CDAC
            cdac_readjust_count += 1
            print(f"\n  FDAC saturated (would need {unclamped_fdac}, clamped to {new_fdac})")
            print(f"  Readjusting CDAC ({cdac_readjust_count}/{MAX_CDAC_READJUST})...")

            # Measure actual offset at the saturated FDAC
            set_dac_and_settle(synth, fdac=new_fdac)
            mean_sat, std_sat, _ = measure_offset(fpga, 15, f"FDAC={new_fdac}")
            print(f"    Offset at FDAC={new_fdac}: {format_deltaf(mean_sat)}")

            # Compute CDAC correction for the residual offset
            cdac_correction = round(-mean_sat / hz_per_cdac)
            if cdac_correction == 0:
                cdac_correction = -1 if mean_sat < 0 else 1
            new_cdac = int(np.clip(cdac + cdac_correction, 0, 255))
            print(f"    CDAC correction: {cdac_correction:+d} → CDAC={new_cdac}")

            set_dac_and_settle(synth, cdac=new_cdac)
            phase['cdac_readjustments'].append({
                'old_cdac': cdac, 'new_cdac': new_cdac,
                'residual_hz': mean_sat, 'cdac_correction': cdac_correction,
            })
            cdac = new_cdac

            # Reset FDAC to midrange and re-measure
            fdac = 128
            set_dac_and_settle(synth, fdac=fdac)
            offset_hz, _, _ = measure_offset(fpga, FINE_MEAS_S, f"CDAC={cdac},FDAC={fdac}")
            print(f"    After CDAC readjust: CDAC={cdac}, FDAC={fdac}, offset={format_deltaf(offset_hz)}")
            continue  # retry fine correction with new CDAC

        print(f"\n  Iteration {iteration + 1}: offset={format_deltaf(offset_hz)}, "
              f"correction={correction:+d} → FDAC={new_fdac}")

        if new_fdac == fdac and abs(offset_hz) > target_hz:
            # Correction too small for integer step — try both neighbors
            best_fdac = fdac
            best_offset = offset_hz
            for c in [max(fdac - 1, 0), min(fdac + 1, 255)]:
                set_dac_and_settle(synth, fdac=c)
                m, _, _ = measure_offset(fpga, FINE_MEAS_S, f"FDAC={c}")
                if abs(m) < abs(best_offset):
                    best_fdac = c
                    best_offset = m
            fdac = best_fdac
            offset_hz = best_offset
            print(f"    Sub-step search: best FDAC={fdac}, offset={format_deltaf(offset_hz)}")
            set_dac_and_settle(synth, fdac=fdac)
            phase['iterations'].append({
                'fdac': fdac, 'offset_hz': offset_hz,
                'method': 'neighbor_search',
            })
            break

        set_dac_and_settle(synth, fdac=new_fdac)
        mean_hz, std_hz, _ = measure_offset(fpga, FINE_MEAS_S, f"FDAC={new_fdac}")
        print(f"    Result: {format_deltaf(mean_hz)} ± {format_deltaf(std_hz)}")

        phase['iterations'].append({
            'fdac': new_fdac, 'offset_hz': mean_hz, 'std_hz': std_hz,
            'correction': correction,
        })

        fdac = new_fdac
        offset_hz = mean_hz

        if abs(offset_hz) < target_hz:
            print(f"  Fine correction converged: {format_deltaf(offset_hz)}")
            break

    phase['final_cdac'] = cdac
    phase['final_fdac'] = fdac
    phase['final_offset_hz'] = offset_hz
    data['phases'].append(phase)
    return cdac, fdac, offset_hz


def phase4_save_and_verify(fpga, synth, data, cdac, fdac, auto_save):
    """Phase 4: Save to EEPROM then verify.

    cal_fst_quit() restores old DAC values, so we must save FIRST,
    then verify the result outside cal mode.
    """
    print(f"\n{'='*70}")
    print("PHASE 4 — EEPROM save & verification")
    print(f"{'='*70}")

    phase = {'name': 'phase4_save_verify'}

    if cdac < 25 or cdac > 230 or fdac < 25 or fdac > 230:
        print(f"  WARNING: DAC values near limits (CDAC={cdac}, FDAC={fdac})")
        print("           TCXO may be aging — consider replacement")

    if not auto_save:
        print(f"\n  Ready to save: CDAC={cdac}, FDAC={fdac}")
        response = input("  Save to EEPROM? [y/N] ").strip().lower()
        if response != 'y':
            print("  Save cancelled.")
            phase['saved'] = False
            data['phases'].append(phase)
            return False, 0.0, 0.0

    # Save (from within cal mode — caller must have called cal_fst_save
    # or we re-enter cal mode to save)
    print(f"  Saving to EEPROM: CDAC={cdac}, FDAC={fdac}...")
    synth.cal_fst_save()  # saves + exits cal mode
    phase['saved'] = True
    phase['cdac'] = cdac
    phase['fdac'] = fdac

    # Verification — now outside cal mode, EEPROM values are active
    print("\n  INT mode, 60s verification...")
    synth.set_frequency(MEASURE_FREQ_HZ)
    synth.set_clock_internal()
    time.sleep(2.0)
    mean_int, std_int, n_int = measure_offset(fpga, 60, "INT verify")
    ppb_final = hz_to_ppb(mean_int)
    print(f"    INT offset: {format_deltaf(mean_int)} ± {format_deltaf(std_int)}"
          f" = {ppb_final:+.1f} ppb (n={n_int})")
    phase['int_verify'] = {'mean_hz': mean_int, 'std_hz': std_int, 'n': n_int, 'ppb': ppb_final}

    # EXT10DIR control
    print("\n  EXT10DIR control measurement, 15s...")
    synth.set_clock_external_10mhz_direct()
    time.sleep(2.0)
    mean_ext, std_ext, n_ext = measure_offset(fpga, 15, "EXT control")
    print(f"    EXT10DIR: {format_deltaf(mean_ext)} ± {format_deltaf(std_ext)} (n={n_ext})")
    phase['ext_control'] = {'mean_hz': mean_ext, 'std_hz': std_ext, 'n': n_ext}

    # Cross-check at other frequencies
    for check_freq in [110e6, 10e6]:
        print(f"\n  Cross-check at {check_freq/1e6:.0f} MHz (INT), 15s...")
        synth.set_clock_internal()
        time.sleep(1.0)
        synth.set_frequency(check_freq)
        time.sleep(2.0)
        flush_history(fpga)
        mean_ck, std_ck, n_ck = measure_offset(fpga, 15, f"{check_freq/1e6:.0f}MHz")
        expected_df = check_freq - MEASURE_FREQ_HZ
        measured_ppm = (mean_ck - expected_df) / check_freq * 1e6
        print(f"    {check_freq/1e6:.0f} MHz: delta_f={format_deltaf(mean_ck)}, "
              f"expected={format_deltaf(expected_df)}, "
              f"error={measured_ppm:+.3f} ppm")
        phase[f'cross_check_{check_freq/1e6:.0f}mhz'] = {
            'freq_hz': check_freq, 'mean_hz': mean_ck, 'std_hz': std_ck,
            'n': n_ck, 'ppm': measured_ppm,
        }

    # Restore measurement frequency
    synth.set_frequency(MEASURE_FREQ_HZ)
    time.sleep(1.0)

    data['phases'].append(phase)
    return True, mean_int, ppb_final


# ── Summary ──────────────────────────────────────────────────────────

def print_summary(data):
    """Print calibration summary."""
    print(f"\n{'='*70}")
    print("CALIBRATION SUMMARY")
    print(f"{'='*70}")

    phases = {p['name']: p for p in data['phases']}

    p0 = phases.get('phase0_verify', {})
    initial_ppb = p0.get('int_baseline', {}).get('ppb', float('nan'))
    initial_cdac = p0.get('initial_cdac', '?')
    initial_fdac = p0.get('initial_fdac', '?')

    print(f"  Initial:  CDAC={initial_cdac}, FDAC={initial_fdac}, "
          f"offset={initial_ppb:+.1f} ppb")

    result = data.get('result', {})
    final_cdac = result.get('final_cdac', '?')
    final_fdac = result.get('final_fdac', '?')
    final_ppb = result.get('final_ppb', float('nan'))

    print(f"  Final:    CDAC={final_cdac}, FDAC={final_fdac}, "
          f"offset={final_ppb:+.1f} ppb")

    if isinstance(initial_ppb, (int, float)) and isinstance(final_ppb, (int, float)):
        if abs(final_ppb) > 0.001:
            improvement = abs(initial_ppb) / abs(final_ppb)
            print(f"  Improvement: {improvement:.0f}x")
        else:
            print(f"  Improvement: >1000x")

    saved = data.get('result', {}).get('saved', False)
    print(f"  Saved to EEPROM: {'Yes' if saved else 'No'}")
    print(f"  Data file: {data.get('metadata', {}).get('filepath', '?')}")


# ── Main calibration routine ─────────────────────────────────────────

def run_calibration(fpga, synth, synth_idn, args):
    """Execute the full calibration procedure."""
    filepath = generate_data_path()
    target_hz = ppb_to_hz(args.target_ppb)
    coarse_threshold_hz = 5.0  # ~80 ppb — skip coarse if already below this

    data = {
        'metadata': {
            'start_utc': datetime.now(timezone.utc).isoformat(),
            'synth_idn': synth_idn,
            'measure_freq_hz': MEASURE_FREQ_HZ,
            'target_ppb': args.target_ppb,
            'target_hz': target_hz,
            'filepath': filepath,
        },
        'phases': [],
    }

    in_cal_mode = False

    try:
        # Setup
        disable_servo_ch1(fpga)
        fpga.set_deltaf_streaming(True)

        # Safety: exit cal mode in case a previous run was interrupted
        try:
            synth.cal_fst_quit()
        except Exception:
            pass

        synth.set_frequency(MEASURE_FREQ_HZ)
        synth.set_amplitude(AMPLITUDE_DBM)
        synth.rf_on()
        time.sleep(1.0)

        # Phase 0 — verification (outside cal mode)
        offset_hz, ppb_initial = phase0_verify(fpga, synth, data)
        save_data(data, filepath)

        if args.measure_only:
            data['result'] = {
                'final_ppb': ppb_initial, 'final_hz': offset_hz,
                'saved': False, 'measure_only': True,
            }
            save_data(data, filepath)
            print_summary(data)
            return data

        # Enter cal mode — stay in it for phases 1-3
        print("\n  Entering calibration mode...")
        synth.cal_fst_init()
        in_cal_mode = True

        # Read current DAC values
        cdac = synth.cal_fst_get_coarse_dac()
        fdac = synth.cal_fst_get_fine_dac()
        print(f"    CDAC={cdac}, FDAC={fdac}")

        # Store initial values in phase0 data
        data['phases'][0]['initial_cdac'] = cdac
        data['phases'][0]['initial_fdac'] = fdac

        # Switch to INT mode for calibration
        synth.set_clock_internal()
        time.sleep(2.0)

        # Phase 1 — CDAC sensitivity (in cal mode)
        hz_per_cdac, offset_hz = phase1_cdac_sensitivity(fpga, synth, data, cdac, fdac)
        save_data(data, filepath)

        if args.dry_run:
            cdac_correction = round(-offset_hz / hz_per_cdac)
            planned_cdac = int(np.clip(cdac + cdac_correction, 0, 255))
            print(f"\n  DRY RUN — Would set CDAC={planned_cdac} (correction {cdac_correction:+d})")
            print(f"           Then fine-tune FDAC around 128")
            synth.cal_fst_quit()
            in_cal_mode = False
            data['result'] = {
                'dry_run': True, 'planned_cdac': planned_cdac,
                'cdac_correction': cdac_correction, 'saved': False,
            }
            save_data(data, filepath)
            return data

        # Phase 2 — coarse correction (in cal mode)
        cdac, offset_hz = phase2_coarse_correction(
            fpga, synth, data, offset_hz, hz_per_cdac, cdac, coarse_threshold_hz)
        save_data(data, filepath)

        # Phase 3 — fine correction (in cal mode, FDAC starts at 128)
        fdac = 128
        cdac, fdac, offset_hz = phase3_fine_correction(
            fpga, synth, data, offset_hz, cdac, fdac, target_hz, hz_per_cdac)
        save_data(data, filepath)

        # Phase 4 — save EEPROM (from cal mode) then verify
        # Note: cal_fst_quit() restores old DAC values, so we must save first
        saved, mean_final, ppb_final = phase4_save_and_verify(
            fpga, synth, data, cdac, fdac, args.auto_save)
        in_cal_mode = False  # cal_fst_save() exits cal mode

        if not saved:
            # User declined save — quit cal mode, discard changes
            synth.cal_fst_quit()
            data['result'] = {
                'final_cdac': cdac, 'final_fdac': fdac,
                'final_ppb': ppb_initial, 'saved': False,
            }
        else:
            data['result'] = {
                'final_cdac': cdac, 'final_fdac': fdac,
                'final_ppb': ppb_final, 'final_hz': mean_final,
                'saved': True,
            }
        save_data(data, filepath)

        print_summary(data)
        return data

    except KeyboardInterrupt:
        print(f"\n\nInterrupted! Data saved to: {filepath}")
        save_data(data, filepath)
        raise

    except Exception:
        save_data(data, filepath)
        print(f"\nData saved to: {filepath}")
        raise

    finally:
        # Safety: exit cal mode if still in it
        if in_cal_mode:
            try:
                synth.cal_fst_quit()
            except Exception:
                pass
        try:
            synth.rf_off()
        except Exception:
            pass


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="IFR 2023A Frequency Standard Calibration via Alpha250",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s                          Full calibration
  %(prog)s --measure-only           Just measure the offset
  %(prog)s --dry-run                Plan without executing
  %(prog)s --auto-save              Save without confirmation
  %(prog)s --target-ppb 50          Tighter convergence target

Requires Level 2 unlock: UTIL 80, password 123456
""",
    )
    parser.add_argument('--host', default='192.168.2.13',
                        help='Alpha250 IP (default: 192.168.2.13)')
    parser.add_argument('--port', type=int, default=36000,
                        help='Alpha250 TCP port (default: 36000)')
    parser.add_argument('--synth-port', default='/dev/ttyUSB0',
                        help='Synthesizer serial port (default: /dev/ttyUSB0)')
    parser.add_argument('--target-ppb', type=float, default=DEFAULT_TARGET_PPB,
                        help=f'Convergence target in ppb (default: {DEFAULT_TARGET_PPB})')
    parser.add_argument('--auto-save', action='store_true',
                        help='Save to EEPROM without confirmation')
    parser.add_argument('--dry-run', action='store_true',
                        help='Characterize sensitivity but do not correct')
    parser.add_argument('--measure-only', action='store_true',
                        help='Only measure current offset, no calibration')
    args = parser.parse_args()

    print(f"\nIFR 2023A Frequency Standard Calibration")
    print(f"  Target: < {args.target_ppb:.0f} ppb ({ppb_to_hz(args.target_ppb):.3f} Hz at {MEASURE_FREQ_HZ/1e6:.1f} MHz)")
    if args.measure_only:
        print("  Mode: measure-only")
    elif args.dry_run:
        print("  Mode: dry-run (characterize only)")

    # Connect
    print("\nConnecting to instruments...")
    fpga = connect_fpga(args.host, args.port)
    print(f"  FPGA: connected to {args.host}")
    synth, synth_idn = connect_synth(args.synth_port)

    try:
        run_calibration(fpga, synth, synth_idn, args)
    finally:
        try:
            synth.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
