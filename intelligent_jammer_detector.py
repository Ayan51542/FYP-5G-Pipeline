"""
INTELLIGENT JAMMER DETECTOR MODULE
Implements lightweight machine learning-based jamming detection:
  - Real-time RF feature extraction
  - Pattern recognition for jamming signatures
  - Confidence scoring for detection
  - Automatic trigger for adaptive defense
  
Anti-Jamming Technique: Proactive jamming identification enables rapid response
through adaptive modulation and spectrum switching.
"""

import numpy as np
import time
from collections import deque
from enum import Enum

# ============================================================================
# DETECTION ENUMS
# ============================================================================

class JammingConfidence(Enum):
    """Confidence level in jamming detection"""
    VERY_LOW = 0.0      # No jamming
    LOW = 0.25          # Weak indicators
    MEDIUM = 0.5        # Some indicators
    HIGH = 0.75         # Strong indicators
    VERY_HIGH = 1.0     # Definite jamming


# ============================================================================
# INTELLIGENT JAMMER DETECTOR
# ============================================================================

class JammerDetector:
    """
    Lightweight ML-based detector for jamming using RF signal features.
    No external dependencies - pure NumPy implementation.
    """
    
    def __init__(self, window_size: int = 20, sensitivity: float = 0.6):
        """
        Args:
            window_size: Number of recent detections to track
            sensitivity: Detection threshold (0.0-1.0, higher = more sensitive)
        """
        
        self.window_size = window_size
        self.sensitivity = sensitivity  # Threshold for jamming decision
        
        # Feature history (for trend analysis)
        self.confidence_history = deque(maxlen=window_size)
        self.feature_history = deque(maxlen=window_size)
        
        # Statistics
        self.detections_count = 0
        self.false_alarm_count = 0
        self.missed_detection_count = 0
        self.last_detection_time = 0.0
        
        # Learned baselines (adaptive)
        self.baseline_noise_power = 1e-10
        self.baseline_flatness = 0.5
        self.baseline_crest_factor = 1.5
    
    def extract_features(self, signal: np.ndarray) -> dict:
        """
        Extract RF features from complex baseband signal.
        
        Args:
            signal: Complex baseband signal (numpy array)
            
        Returns:
            Dictionary of extracted features
        """
        
        if signal is None or len(signal) < 16:
            return self._create_feature_dict(all_zeros=True)
        
        # Time-domain features
        power = np.mean(np.abs(signal) ** 2)
        peak_power = np.max(np.abs(signal) ** 2)
        crest_factor = peak_power / (power + 1e-12)
        
        # Amplitude statistics
        amplitudes = np.abs(signal)
        mean_amp = np.mean(amplitudes)
        std_amp = np.std(amplitudes)
        skewness = (np.mean((amplitudes - mean_amp) ** 3) / 
                   ((std_amp + 1e-12) ** 3))  # Simple skewness
        
        # Frequency-domain features (via FFT)
        fft_result = np.fft.fft(signal)
        psd = np.abs(fft_result) ** 2
        psd_normalized = psd / (np.max(psd) + 1e-12)
        
        # Spectral flatness (entropy-like measure)
        # Flat spectrum = wideband jamming, Peaky spectrum = tone/narrowband
        mean_psd = np.mean(psd_normalized)
        std_psd = np.std(psd_normalized)
        flatness = 1.0 / (1.0 + (std_psd / (mean_psd + 1e-12)))  # 0-1: flat-peaky
        
        # Spectral centroid
        freqs = np.fft.fftfreq(len(fft_result))
        centroid = np.sum(freqs[:len(freqs)//2] * psd_normalized[:len(freqs)//2])
        
        # Spectral spread
        spread = np.sqrt(np.sum(((freqs[:len(freqs)//2] - centroid) ** 2) * 
                               psd_normalized[:len(freqs)//2]))
        
        # PAPR (Peak-to-Average Power Ratio)
        papr = peak_power / (power + 1e-12)
        
        # Signal to Noise Ratio estimation
        # Sort power values; low values ≈ noise
        sorted_power = np.sort(psd_normalized)
        noise_level = np.mean(sorted_power[:max(1, len(sorted_power)//4)])
        signal_level = np.mean(sorted_power[-max(1, len(sorted_power)//4):])
        snr_estimated = (signal_level - noise_level) / (noise_level + 1e-12)
        
        features = self._create_feature_dict(
            power=power,
            crest_factor=crest_factor,
            flatness=flatness,
            spectral_entropy=self._compute_entropy(psd_normalized),
            skewness=skewness,
            papr=papr,
            mean_amplitude=mean_amp,
            std_amplitude=std_amp,
            spectral_spread=spread,
            snr_estimated=snr_estimated
        )
        
        self.feature_history.append(features)
        return features
    
    def _create_feature_dict(self, all_zeros: bool = False, **kwargs) -> dict:
        """Create feature dictionary"""
        
        if all_zeros:
            return {
                'power': 0.0,
                'crest_factor': 0.0,
                'flatness': 0.5,
                'spectral_entropy': 0.0,
                'skewness': 0.0,
                'papr': 0.0,
                'mean_amplitude': 0.0,
                'std_amplitude': 0.0,
                'spectral_spread': 0.0,
                'snr_estimated': 0.0,
                'timestamp': time.time()
            }
        
        return {
            **kwargs,
            'timestamp': time.time()
        }
    
    def detect_jamming(self, signal: np.ndarray) -> dict:
        """
        Detect jamming using extracted features and ML scoring.
        
        Args:
            signal: Complex baseband signal
            
        Returns:
            Detection result dictionary with confidence and reasoning
        """
        
        # Extract features
        features = self.extract_features(signal)
        
        # Compute detection scores (0-1 scale)
        scores = []
        reasons = []
        
        # Score 1: High power indicates potential jamming
        power_score = self._score_power(features['power'])
        if power_score > 0.3:
            scores.append(power_score * 0.15)  # Weight: 15%
            reasons.append(f"HIGH_POWER({power_score:.2f})")
        
        # Score 2: Flat spectrum = wideband jamming
        flatness_score = self._score_flatness(features['flatness'])
        if flatness_score > 0.3:
            scores.append(flatness_score * 0.25)  # Weight: 25%
            reasons.append(f"FLAT_SPECTRUM({flatness_score:.2f})")
        
        # Score 3: High PAPR or crest factor = impulse jamming
        crest_score = self._score_crest_factor(features['crest_factor'])
        if crest_score > 0.3:
            scores.append(crest_score * 0.20)  # Weight: 20%
            reasons.append(f"HIGH_CREST({crest_score:.2f})")
        
        # Score 4: Spectral entropy patterns
        entropy_score = self._score_entropy(features['spectral_entropy'])
        if entropy_score > 0.3:
            scores.append(entropy_score * 0.20)  # Weight: 20%
            reasons.append(f"ENTROPY({entropy_score:.2f})")
        
        # Score 5: SNR degradation
        snr_score = self._score_snr(features['snr_estimated'])
        if snr_score > 0.3:
            scores.append(snr_score * 0.15)  # Weight: 15%
            reasons.append(f"LOW_SNR({snr_score:.2f})")
        
        # Aggregate score
        if scores:
            confidence = np.mean(scores)
        else:
            confidence = 0.0
        
        # Apply trend detection (increasing interference → higher confidence)
        if len(self.confidence_history) >= 3:
            trend = (self.confidence_history[-1] - 
                    self.confidence_history[-3])  # Trend over 3 samples
            if trend > 0.1:
                confidence *= 1.2  # Boost if trend is increasing
                reasons.append(f"TREND_UP({trend:.2f})")
        
        # Clamp to 0-1
        confidence = min(1.0, max(0.0, confidence))
        
        # Decision
        is_jammed = confidence > self.sensitivity
        
        # Record history
        self.confidence_history.append(confidence)
        if is_jammed:
            self.detections_count += 1
            self.last_detection_time = time.time()
        
        result = {
            'is_jammed': is_jammed,
            'confidence': confidence,
            'confidence_enum': self._score_to_enum(confidence),
            'features': features,
            'scoring_reasons': reasons,
            'threshold': self.sensitivity,
            'timestamp': time.time()
        }
        
        return result
    
    def _score_power(self, power: float) -> float:
        """
        Score based on signal power.
        Higher power → higher jamming likelihood
        """
        
        # Normalize to baseline
        if self.baseline_noise_power > 0:
            power_ratio = power / (self.baseline_noise_power * 100)
        else:
            power_ratio = 0.0
        
        # Score: sigmoid-like curve
        score = 1.0 / (1.0 + np.exp(-2.0 * (power_ratio - 0.5)))
        return min(1.0, score)
    
    def _score_flatness(self, flatness: float) -> float:
        """
        Score based on spectrum flatness.
        Flatness ~ 1 (flat) = wideband jamming
        Flatness ~ 0 (peaky) = narrowband or OFDM
        """
        
        # Flat spectrum = high jamming score
        score = flatness  # Already 0-1
        return score
    
    def _score_crest_factor(self, crest_factor: float) -> float:
        """
        Score based on crest factor.
        High crest = impulse/burst jamming
        """
        
        # Normalize: typical signals have CF ~ 1-2, jamming ~ 3-5+
        normalized_cf = max(0, min(1.0, (crest_factor - 2.0) / 3.0))
        return normalized_cf
    
    def _score_entropy(self, entropy: float) -> float:
        """
        Score based on spectral entropy.
        High entropy = random/noise-like = jamming
        """
        
        # Entropy 0-1 scale
        return min(1.0, entropy)
    
    def _score_snr(self, snr: float) -> float:
        """
        Score based on estimated SNR.
        Low SNR = degraded channel (jamming)
        """
        
        # SNR below 0 = high jamming likelihood
        if snr < 0:
            return 1.0 - (1.0 / (1.0 + np.exp(snr)))  # Sigmoid
        else:
            return 0.0
    
    def _compute_entropy(self, psd_normalized: np.ndarray) -> float:
        """
        Compute Shannon entropy of PSD (normalized to 0-1).
        High entropy = random/uniform spectrum
        """
        
        psd_norm = psd_normalized / (np.sum(psd_normalized) + 1e-12)
        entropy = -np.sum(psd_norm * np.log2(psd_norm + 1e-10))
        
        # Normalize to 0-1 (max entropy for n bins = log2(n))
        max_entropy = np.log2(len(psd_norm))
        entropy_normalized = entropy / (max_entropy + 1e-12)
        
        return min(1.0, entropy_normalized)
    
    def _score_to_enum(self, confidence: float) -> JammingConfidence:
        """Convert confidence score to enum"""
        
        if confidence > 0.85:
            return JammingConfidence.VERY_HIGH
        elif confidence > 0.65:
            return JammingConfidence.HIGH
        elif confidence > 0.4:
            return JammingConfidence.MEDIUM
        elif confidence > 0.15:
            return JammingConfidence.LOW
        else:
            return JammingConfidence.VERY_LOW
    
    def get_average_confidence(self) -> float:
        """Get average confidence from recent history"""
        
        if len(self.confidence_history) == 0:
            return 0.0
        
        return np.mean(list(self.confidence_history))
    
    def get_diagnostics(self) -> dict:
        """Return detector diagnostics"""
        
        avg_conf = self.get_average_confidence()
        
        return {
            'detections': self.detections_count,
            'false_alarms': self.false_alarm_count,
            'missed_detections': self.missed_detection_count,
            'average_confidence': avg_conf,
            'confidence_trend': list(self.confidence_history)[-5:] if self.confidence_history else [],
            'last_detection_time': self.last_detection_time,
            'sensitivity_threshold': self.sensitivity,
            'baseline_noise_power': self.baseline_noise_power
        }
    
    def update_baseline(self, clean_signal: np.ndarray):
        """
        Update baseline noise characteristics from a clean signal.
        Used for adaptation after channel conditions change.
        """
        
        if clean_signal is None or len(clean_signal) < 16:
            return
        
        features = self.extract_features(clean_signal)
        
        self.baseline_noise_power = features['power']
        self.baseline_flatness = features['flatness']
        self.baseline_crest_factor = features['crest_factor']
    
    def reset(self):
        """Reset all statistics"""
        self.confidence_history.clear()
        self.feature_history.clear()
        self.detections_count = 0
        self.false_alarm_count = 0
        self.missed_detection_count = 0
        self.last_detection_time = 0.0


# ============================================================================
# GLOBAL INSTANCE
# ============================================================================

jammer_detector = JammerDetector(window_size=20, sensitivity=0.6)


if __name__ == "__main__":
    print("\n[TEST] Intelligent Jammer Detector Module\n")
    
    detector = JammerDetector(sensitivity=0.6)
    
    # Test 1: Clean signal
    print("=== CLEAN SIGNAL ===")
    clean_signal = np.random.normal(0, 1e-5, 256) + 1j*np.random.normal(0, 1e-5, 256)
    result = detector.detect_jamming(clean_signal)
    print(f"  Jammed: {result['is_jammed']}")
    print(f"  Confidence: {result['confidence']:.2%}")
    print(f"  Level: {result['confidence_enum'].name}")
    print(f"  Reasons: {result['scoring_reasons']}")
    
    # Test 2: Wideband jamming
    print("\n=== WIDEBAND JAMMING ===")
    jammed_signal = np.random.normal(0, 1e-7, 256) + 1j*np.random.normal(0, 1e-7, 256)
    result = detector.detect_jamming(jammed_signal)
    print(f"  Jammed: {result['is_jammed']}")
    print(f"  Confidence: {result['confidence']:.2%}")
    print(f"  Level: {result['confidence_enum'].name}")
    print(f"  Reasons: {result['scoring_reasons']}")
    
    # Test 3: Impulse jamming
    print("\n=== IMPULSE JAMMING ===")
    impulse_signal = np.zeros(256, dtype=complex)
    impulse_signal[::16] = np.random.normal(0, 0.1, 16) + 1j*np.random.normal(0, 0.1, 16)
    result = detector.detect_jamming(impulse_signal)
    print(f"  Jammed: {result['is_jammed']}")
    print(f"  Confidence: {result['confidence']:.2%}")
    print(f"  Level: {result['confidence_enum'].name}")
    
    # Diagnostics
    print("\n=== DIAGNOSTICS ===")
    diag = detector.get_diagnostics()
    for key, value in diag.items():
        print(f"  {key}: {value}")
