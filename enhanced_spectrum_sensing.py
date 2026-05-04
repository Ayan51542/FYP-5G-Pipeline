"""
ENHANCED SPECTRUM SENSING MODULE
Implements intelligent spectrum sensing with:
  - Improved Markov-based environment model
  - Energy detection with adaptive thresholds
  - Jamming signature detection
  - Automatic channel switching recommendations
  
Anti-Jamming Technique: Proactive spectrum awareness allows secondary users to avoid
or quickly detect jammed channels and switch to better alternatives.
"""

import numpy as np
import time
from collections import deque
from enum import Enum

# ============================================================================
# SPECTRUM STATE ENUM
# ============================================================================

class ChannelState(Enum):
    """Possible channel states"""
    IDLE = 0        # No primary user, low interference
    BUSY = 1        # Primary user detected, normal interference
    JAMMED = 2      # High interference (likely jamming)
    UNKNOWN = 3     # Insufficient data


class InterferenceType(Enum):
    """Types of detected interference"""
    NONE = 0
    AWGN = 1           # Additive White Gaussian Noise
    NARROWBAND = 2     # Single tone interference
    WIDEBAND = 3       # Spread spectrum (jamming)
    IMPULSE = 4        # Burst/Pulse interference
    UNKNOWN = 5


# ============================================================================
# ENHANCED SPECTRUM SENSING ENGINE
# ============================================================================

class SpectrumSensor:
    """
    Advanced spectrum sensing with adaptive thresholds and jamming detection.
    """
    
    def __init__(self,
                 idle_threshold: float = 1e-10,
                 noise_floor: float = 1e-10,
                 markov_threshold: float = 3.0,
                 window_size: int = 10):
        """
        Args:
            idle_threshold: Power below this = channel IDLE
            noise_floor: Baseline environmental noise
            markov_threshold: Multiplier for Markov threshold adaptation
            window_size: Number of recent measurements to track
        """
        
        self.idle_threshold = idle_threshold
        self.noise_floor = noise_floor
        self.markov_threshold = markov_threshold
        self.window_size = window_size
        
        # Markov state machine for primary user presence
        self.markov_state = 0  # 0=IDLE, 1=BUSY
        self.markov_transition_matrix = [
            [0.85, 0.15],  # P(IDLE→IDLE)=0.85, P(IDLE→BUSY)=0.15
            [0.40, 0.60]   # P(BUSY→IDLE)=0.40, P(BUSY→BUSY)=0.60
        ]
        
        # Adaptive threshold tuning
        self.threshold_history = deque(maxlen=window_size)
        self.power_history = deque(maxlen=window_size * 2)
        self.state_history = deque(maxlen=window_size)
        
        # Jamming indicators
        self.jam_indicators = deque(maxlen=window_size)
        self.last_jam_detection_time = 0.0
        
        # Statistics
        self.measurements_count = 0
        self.jam_detections = 0
        self.false_alarms = 0
        
    def sense_channel(self, signal: np.ndarray) -> dict:
        """
        Perform spectrum sensing on received signal.
        
        Args:
            signal: Complex baseband signal samples
            
        Returns:
            Dictionary with sensing results
        """
        
        if signal is None or len(signal) == 0:
            return self._create_result(ChannelState.UNKNOWN, 0.0)
        
        self.measurements_count += 1
        
        # Estimate signal power (energy detection)
        power = self._estimate_power(signal)
        self.power_history.append(power)
        
        # Detect interference type
        interference_type = self._detect_interference_type(signal)
        
        # Estimate SINR
        sinr_db = self._estimate_sinr(power)
        
        # Markov state transition
        self._update_markov_state(power)
        
        # Adaptive threshold
        adaptive_threshold = self._compute_adaptive_threshold()
        self.threshold_history.append(adaptive_threshold)
        
        # Determine channel state
        state = self._classify_state(power, adaptive_threshold, interference_type)
        self.state_history.append(state)
        
        # Check for jamming indicators
        is_jammed = self._check_jamming_indicators(signal, power, interference_type, sinr_db)
        
        if is_jammed:
            self.jam_detections += 1
            self.last_jam_detection_time = time.time()
            self.jam_indicators.append(True)
        else:
            self.jam_indicators.append(False)
        
        result = self._create_result(
            state,
            power,
            sinr_db=sinr_db,
            threshold=adaptive_threshold,
            interference_type=interference_type,
            is_jammed=is_jammed,
            markov_state=self.markov_state
        )
        
        return result
    
    def _estimate_power(self, signal: np.ndarray) -> float:
        """
        Estimate instantaneous power using energy detection.
        Power = (1/N) * Σ|x[n]|²
        """
        if len(signal) == 0:
            return 0.0
        
        energy = np.mean(np.abs(signal) ** 2)
        return float(energy)
    
    def _detect_interference_type(self, signal: np.ndarray) -> InterferenceType:
        """
        Classify type of interference based on spectral characteristics.
        """
        
        if len(signal) < 32:
            return InterferenceType.UNKNOWN
        
        # Compute power spectral density via FFT
        fft_result = np.fft.fft(signal)
        psd = np.abs(fft_result) ** 2
        psd_normalized = psd / (np.max(psd) + 1e-12)
        
        # Metrics
        peak_value = np.max(psd_normalized)
        mean_value = np.mean(psd_normalized)
        std_value = np.std(psd_normalized)
        crest_factor = peak_value / (mean_value + 1e-12)
        
        # Narrowband indicator: sharp peak
        if crest_factor > 5.0 and peak_value > 0.5:
            return InterferenceType.NARROWBAND
        
        # Wideband indicator: flat spectrum
        elif std_value < 0.1 and mean_value > 0.01:
            return InterferenceType.WIDEBAND
        
        # Impulse indicator: occasional spikes
        elif crest_factor > 2.0 and peak_value > 0.3:
            return InterferenceType.IMPULSE
        
        # AWGN indicator: Gaussian-like
        elif std_value > 0.05:
            return InterferenceType.AWGN
        
        else:
            return InterferenceType.NONE
    
    def _estimate_sinr(self, power: float) -> float:
        """
        Estimate SINR based on current power and noise floor.
        SINR = Power / Noise_Floor (in linear scale)
        """
        
        sinr_linear = power / (self.noise_floor + 1e-12)
        sinr_db = 10.0 * np.log10(max(sinr_linear, 1e-6))
        
        return sinr_db
    
    def _update_markov_state(self, power: float):
        """
        Update Markov state based on power observations.
        """
        
        busy_threshold = self.idle_threshold * self.markov_threshold * 10.0
        
        # Emit probability based on power
        if power > busy_threshold:
            prob_busy_emission = 0.8
        else:
            prob_busy_emission = 0.2
        
        # Generate random transition
        old_state = self.markov_state
        
        if np.random.random() < self.markov_transition_matrix[old_state][1 - old_state]:
            self.markov_state = 1 - self.markov_state
        
        return old_state != self.markov_state
    
    def _compute_adaptive_threshold(self) -> float:
        """
        Compute adaptive detection threshold based on recent power measurements.
        Uses Neyman-Pearson criterion.
        
        Threshold = (1 + margin_factor) * average_noise_power
        """
        
        if len(self.power_history) < 5:
            # Default threshold during initialization
            return self.idle_threshold * self.markov_threshold * 2.0
        
        # Use lowest power readings as noise estimate
        recent_powers = list(self.power_history)
        recent_powers.sort()
        noise_estimate = np.mean(recent_powers[:max(1, len(recent_powers)//3)])
        
        # Adaptive threshold with margin
        margin_factor = 3.0  # Tunable detection sensitivity
        adaptive_threshold = (1.0 + margin_factor / 10.0) * noise_estimate
        
        return adaptive_threshold
    
    def _classify_state(self, 
                       power: float, 
                       threshold: float,
                       interference_type: InterferenceType) -> ChannelState:
        """
        Classify channel state based on power and interference type.
        """
        
        # Jamming signatures take priority
        if interference_type in [InterferenceType.WIDEBAND, InterferenceType.IMPULSE]:
            return ChannelState.JAMMED
        
        # Power-based classification
        if power < threshold:
            return ChannelState.IDLE
        elif power < threshold * 2:
            return ChannelState.BUSY
        else:
            return ChannelState.JAMMED
    
    def _check_jamming_indicators(self,
                                 signal: np.ndarray,
                                 power: float,
                                 interference_type: InterferenceType,
                                 sinr_db: float) -> bool:
        """
        Multi-factor jamming detection.
        
        Returns: True if jamming is likely detected
        """
        
        jam_score = 0.0
        max_score = 5.0
        
        # Factor 1: Wideband or impulse interference (strong indicator)
        if interference_type in [InterferenceType.WIDEBAND, InterferenceType.IMPULSE]:
            jam_score += 2.0
        
        # Factor 2: Very high power
        if power > self.idle_threshold * 100:
            jam_score += 1.5
        
        # Factor 3: SINR unusually low
        if sinr_db < -10.0:
            jam_score += 1.0
        
        # Factor 4: Crest factor indicates impulse
        if len(signal) > 16:
            signal_power = np.mean(np.abs(signal) ** 2)
            crest_factor = np.max(np.abs(signal)) ** 2 / (signal_power + 1e-12)
            if crest_factor > 3.0:
                jam_score += 0.5
        
        # Decision threshold
        return jam_score > 1.5
    
    def _create_result(self, 
                      state: ChannelState,
                      power: float,
                      sinr_db: float = 0.0,
                      threshold: float = 0.0,
                      interference_type: InterferenceType = InterferenceType.UNKNOWN,
                      is_jammed: bool = False,
                      markov_state: int = 0) -> dict:
        """Create result dictionary"""
        
        return {
            'state': state,
            'power': power,
            'sinr_db': sinr_db,
            'threshold': threshold,
            'interference_type': interference_type,
            'is_jammed': is_jammed,
            'markov_state': markov_state,
            'timestamp': time.time(),
            'measurement_count': self.measurements_count
        }
    
    def should_transmit(self, signal: np.ndarray = None) -> bool:
        """
        Quick decision: should we transmit now?
        
        Returns: True if channel appears safe
        """
        
        if signal is not None:
            result = self.sense_channel(signal)
        else:
            # Use last state if no signal provided
            if len(self.state_history) == 0:
                return True  # Default: transmit
            state = self.state_history[-1]
            result = {'state': state, 'is_jammed': False}
        
        # Transmit only if IDLE and not jammed
        return (result['state'] == ChannelState.IDLE and 
                not result['is_jammed'])
    
    def get_diagnostics(self) -> dict:
        """Return sensing diagnostics"""
        
        avg_power = np.mean(list(self.power_history)) if self.power_history else 0.0
        avg_threshold = np.mean(list(self.threshold_history)) if self.threshold_history else 0.0
        
        jam_rate = (self.jam_detections / max(1, self.measurements_count) 
                   if self.measurements_count > 0 else 0.0)
        
        last_state = self.state_history[-1] if self.state_history else ChannelState.UNKNOWN
        
        return {
            'markov_state': self.markov_state,
            'last_channel_state': last_state,
            'average_power': avg_power,
            'average_threshold': avg_threshold,
            'measurements': self.measurements_count,
            'jam_detections': self.jam_detections,
            'jam_detection_rate': jam_rate,
            'recent_jam_indicators': list(self.jam_indicators),
            'last_jam_time': self.last_jam_detection_time
        }
    
    def reset(self):
        """Reset all counters"""
        self.markov_state = 0
        self.measurements_count = 0
        self.jam_detections = 0
        self.power_history.clear()
        self.state_history.clear()
        self.threshold_history.clear()
        self.jam_indicators.clear()
        self.last_jam_detection_time = 0.0


# ============================================================================
# GLOBAL INSTANCE
# ============================================================================

spectrum_sensor = SpectrumSensor(
    idle_threshold=1e-10,
    noise_floor=1e-10,
    markov_threshold=3.0,
    window_size=10
)


if __name__ == "__main__":
    print("\n[TEST] Enhanced Spectrum Sensing Module\n")
    
    sensor = SpectrumSensor()
    
    # Test 1: Clean signal
    print("=== CLEAN SIGNAL ===")
    clean_signal = np.random.normal(0, 1e-5, 256) + 1j*np.random.normal(0, 1e-5, 256)
    result = sensor.sense_channel(clean_signal)
    print(f"  State: {result['state']}")
    print(f"  Power: {result['power']:.2e}")
    print(f"  SINR: {result['sinr_db']:.1f} dB")
    print(f"  Is Jammed: {result['is_jammed']}")
    
    # Test 2: Wideband jamming
    print("\n=== WIDEBAND JAMMING ===")
    jammed_signal = np.random.normal(0, 1e-7, 256) + 1j*np.random.normal(0, 1e-7, 256)
    result = sensor.sense_channel(jammed_signal)
    print(f"  State: {result['state']}")
    print(f"  Power: {result['power']:.2e}")
    print(f"  Is Jammed: {result['is_jammed']}")
    
    # Diagnostics
    print("\n=== DIAGNOSTICS ===")
    diag = sensor.get_diagnostics()
    for key, value in diag.items():
        print(f"  {key}: {value}")
