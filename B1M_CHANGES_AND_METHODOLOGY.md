# B1 vs B1M: Change Log and ML Jamming-Detection Methodology

## Purpose
This document explains what changed from `b1.py` (baseline base station) to `b1M.py` (ML-enabled base station), and how the runtime Random Forest + XGBoost jamming prediction pipeline works through `threat_model_runtime.py`.

---

## 1. High-level difference

### `b1.py` (baseline)
- Simulates jamming using `JAMMING_LAST_SEEN` + bit-flip corruption.
- Performs energy extraction and standard routing/queue logic.
- Generates base station analytics plots.
- **No trained-model inference pipeline**.

### `b1M.py` (ML-enabled)
- Adds runtime ML scoring for each packet using:
  - `random_forest_model.joblib`
  - `xgboost_model.joblib`
- Adds threat probability decision and ML threat logging.
- Adds ML counters and ML timeline plot.
- Adds packet decoding helpers and signal-based shutdown handling.

---

## 2. Code-level changes from `b1.py` to `b1M.py`

## 2.1 New imports
- `from threat_model_runtime import ThreatModelRuntime`
- `import signal`

These enable runtime model inference and graceful signal-triggered shutdown.

## 2.2 New ML configuration
`b1M.py` adds:
- `ML_THREAT_THRESHOLD = 0.65`
- `ML_ENFORCE_BLOCKING = False`

Meaning:
- A packet is marked as threat when ensemble probability `>= 0.65`.
- Blocking is optional and currently disabled by default.

## 2.3 New ML/global state
`b1M.py` adds:
- `THREAT_HISTORY` and `THREAT_HISTORY_LOCK`
- `ML_RUNTIME = ThreatModelRuntime(...)`
- `SERVER_SOCKET` (used by shutdown routine)

These track threat scores over time and keep model runtime initialized once.

## 2.4 New helper functions in `b1M.py`
Added functions not present in `b1.py`:
- `submit_frame_task(...)`
- `request_shutdown(...)`
- `_signal_handler(...)`
- `aes_gcm_decrypt(...)`
- `split_optional_sensing_header(...)`
- `decode_payload_with_optional_header(...)`

Key effect:
- robust packet decode path (supports optional 8-byte sensing-energy header),
- explicit shutdown control,
- packet task dispatch helper.

## 2.5 `process_incoming_frame(...)` enhancement
In `b1M.py`, processing now includes:
1. Existing jamming simulation (bit flipping when jammer active).
2. Decode payload via optional sensing-header-aware logic.
3. Reconstruct OFDM signal and energy.
4. Call `ML_RUNTIME.score_packet(...)`.
5. Store/update ML metrics:
   - `ml_scored`
   - `ml_alerts`
   - `ml_blocked`
6. Log threat events:
   - `[ML-THREAT] src->dst (RF=..., XGB=..., Ensemble=...)`
7. Optional block action if `ML_ENFORCE_BLOCKING=True`.
8. Continue existing primary-priority and routing behavior.

## 2.6 Reporting changes
`b1M.py` adds one new plot compared to `b1.py`:
- **Plot 11: ML Threat Probability Timeline**

This plot shows ensemble threat probability vs time with threshold line.

## 2.7 Main loop / shutdown behavior
`b1M.py` main flow introduces signal handlers and explicit shutdown routine:
- handles SIGINT/SIGTERM through `_signal_handler`,
- closes client/server sockets in `request_shutdown`,
- exports report on exit.

---

## 3. How `threat_model_runtime.py` works

## 3.1 Model loading
`ThreatModelRuntime._load_models()`:
- loads `random_forest_model.joblib` and `xgboost_model.joblib`,
- disables runtime inference if files are missing,
- loads once at startup (not per packet).

## 3.2 Runtime feature construction
`_build_feature_row(...)` computes a feature vector from OFDM signal / sensing energy:
- FFT-derived magnitude statistics,
- power/log-power derived values,
- scan type flag.

### Feature fields used in code
`FEATURE_COLS`:
1. `freq1`
2. `noise`
3. `max_magnitude`
4. `total_gain_db`
5. `base_pwr_db`
6. `rssi`
7. `relpwr_db`
8. `avgpwr_db`
9. `rssi_dbm`
10. `scan_type`

> Note: the current runtime implementation uses **10 features**.  
> The loaded RF/XGBoost artifacts in this repo also report `n_features_in_ = 10`.

## 3.3 Adaptive scaling
`AdaptiveFeatureScaler`:
- keeps rolling history (default 512 rows),
- computes runtime mean/std,
- applies z-score transform before inference.

This avoids requiring a serialized offline scaler file at runtime.

## 3.4 Threat scoring logic
`score_packet(...)`:
- computes RF probability (`predict_proba[...,1]`),
- computes XGB probability (`predict_proba[...,1]`),
- combines by simple average:
  - `ensemble_probability = (rf + xgb) / 2`
- final decision:
  - `is_threat = ensemble_probability >= threshold`

Returns `ThreatPrediction` with RF, XGB, ensemble, and binary threat flag.

---

## 4. Complete runtime scenario (end-to-end)

1. Sender/receiver/jammer register at base station.
2. Base station receives packet.
3. If jammer is active, payload may be corrupted (existing PHY attack simulation).
4. Base station decodes packet (supports optional sensing-energy header).
5. OFDM/energy features are reconstructed.
6. Runtime ML predicts threat probability (RF + XGB ensemble).
7. If predicted as threat:
   - event logged,
   - counters updated,
   - optional block if enforcement enabled.
8. Primary/secondary priority logic is applied.
9. Packet is delivered locally, hopped to neighbor, or queued.
10. On shutdown, report artifacts are saved (plots + stats text).

---

## 5. Plot outputs in `b1M.py`

`b1M.py` retains baseline plots and adds ML visibility:

1. Energy Timeline  
2. Average Energy by Source  
3. Jamming Activity Detection  
4. Session Statistics  
5. Traffic Distribution (Primary vs Secondary)  
6. Delivery Metrics  
7. Spectrum Sensing Overlay  
8. Jamming Hotspot Heatmap  
9. Channel Occupancy Before/After Jamming  
10. Spectrum Utilization Efficiency  
11. **ML Threat Probability Timeline** *(new in B1M)*

---

## 6. Practical interpretation

- `b1M.py` is not only detecting jammer activity by heuristic timing (`JAMMING_LAST_SEEN`) but also by **learned RF behavior patterns** via trained models.
- The model output is probabilistic (continuous), and thresholding converts it to an actionable decision.
- The additional ML timeline plot gives visibility into how threat confidence evolves during a session.
