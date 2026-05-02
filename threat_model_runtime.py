import math
import os
import warnings
from collections import deque
from dataclasses import dataclass
from typing import Optional

import joblib
import numpy as np

FEATURE_COLS = [
    "freq1",
    "noise",
    "max_magnitude",
    "total_gain_db",
    "base_pwr_db",
    "rssi",
    "relpwr_db",
    "avgpwr_db",
    "rssi_dbm",
    "scan_type",
]

EPSILON = 1e-12


def _safe_db(value: float) -> float:
    return float(10.0 * np.log10(max(value, EPSILON)))


class AdaptiveFeatureScaler:
    def __init__(self, feature_count: int, history_size: int = 512) -> None:
        self.feature_count = feature_count
        self.history = deque(maxlen=history_size)

    def transform(self, feature_row: np.ndarray) -> np.ndarray:
        row = np.asarray(feature_row, dtype=np.float32).reshape(-1)
        if row.size != self.feature_count:
            raise ValueError(
                f"Feature length mismatch: expected {self.feature_count}, got {row.size}"
            )

        self.history.append(row.copy())
        if len(self.history) == 1:
            return np.zeros((1, self.feature_count), dtype=np.float32)

        history_matrix = np.vstack(self.history)
        means = history_matrix.mean(axis=0)
        stds = history_matrix.std(axis=0)
        stds[stds < 1e-6] = 1.0
        scaled = ((row - means) / stds).astype(np.float32)
        return scaled.reshape(1, -1)


@dataclass(frozen=True)
class ThreatPrediction:
    rf_probability: float
    xgb_probability: float
    ensemble_probability: float
    is_threat: bool


class ThreatModelRuntime:
    def __init__(
        self,
        model_dir: str,
        threshold: float = 0.65,
        history_size: int = 512,
    ) -> None:
        self.model_dir = model_dir
        self.threshold = float(threshold)
        self.scaler = AdaptiveFeatureScaler(feature_count=len(FEATURE_COLS), history_size=history_size)
        self.rf_model = None
        self.xgb_model = None
        self.enabled = False
        self.last_error = None
        self._load_models()

    def _load_models(self) -> None:
        rf_path = os.path.join(self.model_dir, "random_forest_model.joblib")
        xgb_path = os.path.join(self.model_dir, "xgboost_model.joblib")

        if not os.path.isfile(rf_path) or not os.path.isfile(xgb_path):
            print("[ML] Threat models not found; ML threat detection disabled.")
            self.enabled = False
            return

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning)
            warnings.filterwarnings("ignore", message="Trying to unpickle estimator.*")
            self.rf_model = joblib.load(rf_path)
            self.xgb_model = joblib.load(xgb_path)

        self.enabled = True
        print(f"[ML] Loaded RF + XGBoost threat models from {self.model_dir}")

    def _build_feature_row(
        self,
        ofdm_signal: np.ndarray,
        sensing_energy: Optional[float],
        scan_type: float,
    ) -> np.ndarray:
        signal = np.asarray(ofdm_signal if ofdm_signal is not None else [], dtype=np.complex64).reshape(-1)

        if signal.size > 0:
            fft_magnitude = np.abs(np.fft.fft(signal)).astype(np.float64)
            max_magnitude = float(np.max(fft_magnitude))
            noise_floor = float(np.median(fft_magnitude))
            avg_magnitude = float(np.mean(fft_magnitude))
            dominant_bin = int(np.argmax(fft_magnitude))
            freq1 = float(dominant_bin / max(len(fft_magnitude) - 1, 1))
            signal_power = float(np.mean(np.abs(signal) ** 2))
        else:
            signal_power = float(sensing_energy) if sensing_energy is not None else 0.0
            max_magnitude = max(signal_power, EPSILON)
            noise_floor = max(max_magnitude * 0.25, EPSILON)
            avg_magnitude = max_magnitude
            freq1 = 0.0

        if sensing_energy is not None and math.isfinite(sensing_energy) and sensing_energy > 0.0:
            signal_power = float(sensing_energy)

        base_pwr_db = _safe_db(signal_power)
        avgpwr_db = _safe_db(max(signal_power, max_magnitude))
        relpwr_db = _safe_db(max_magnitude / max(noise_floor, EPSILON))
        total_gain_db = _safe_db(avg_magnitude / max(noise_floor, EPSILON))
        rssi = base_pwr_db + 95.0
        rssi_dbm = rssi - 95.0

        return np.array(
            [
                freq1,
                noise_floor,
                max_magnitude,
                total_gain_db,
                base_pwr_db,
                rssi,
                relpwr_db,
                avgpwr_db,
                rssi_dbm,
                float(scan_type),
            ],
            dtype=np.float32,
        )

    def score_packet(
        self,
        ofdm_signal: np.ndarray,
        sensing_energy: Optional[float],
        scan_type: float = 1.0,
    ) -> Optional[ThreatPrediction]:
        if not self.enabled:
            return None

        feature_row = self._build_feature_row(
            ofdm_signal=ofdm_signal,
            sensing_energy=sensing_energy,
            scan_type=scan_type,
        )
        scaled_row = self.scaler.transform(feature_row)

        try:
            rf_probability = float(self.rf_model.predict_proba(scaled_row)[0][1])
            xgb_probability = float(self.xgb_model.predict_proba(scaled_row)[0][1])
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            print(f"[ML] Threat inference failed: {exc}")
            return None

        rf_probability = float(np.clip(rf_probability, 0.0, 1.0))
        xgb_probability = float(np.clip(xgb_probability, 0.0, 1.0))
        ensemble_probability = float((rf_probability + xgb_probability) / 2.0)
        is_threat = ensemble_probability >= self.threshold

        return ThreatPrediction(
            rf_probability=rf_probability,
            xgb_probability=xgb_probability,
            ensemble_probability=ensemble_probability,
            is_threat=is_threat,
        )
