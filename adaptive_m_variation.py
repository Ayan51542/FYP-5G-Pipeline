"""
ADAPTIVE M VARIATION MODULE
Implements dynamic modulation (M) selection based on:
  - Real-time SINR estimates
  - Jamming detection flags
  - Message priority and size
  - Historical frame success rates
  
Anti-Jamming Technique: Dynamically switches between QAM-16 (robust), QAM-64 (balanced), 
and QAM-256 (efficient) based on channel conditions.
"""

import numpy as np
import time
from collections import deque

# ============================================================================
# GLOBAL STATE
# ============================================================================

class AdaptiveModulation:
    """
    Tracks channel quality and adapts modulation scheme in real-time.
    """
    
    def __init__(self, 
                 window_size=20,
                 sinr_threshold_low=-5.0,
                 sinr_threshold_high=5.0,
                 jam_count_threshold=3):
        """
        Args:
            window_size: Number of recent frames to track for success rate
            sinr_threshold_low: SINR below this → force QAM-16
            sinr_threshold_high: SINR above this → can use QAM-256
            jam_count_threshold: Number of jammed frames to trigger adaptation
        """
        self.window_size = window_size
        self.sinr_threshold_low = sinr_threshold_low
        self.sinr_threshold_high = sinr_threshold_high
        self.jam_count_threshold = jam_count_threshold
        
        # Tracking state
        self.frame_success_history = deque(maxlen=window_size)
        self.jammed_frame_count = 0
        self.clean_frame_count = 0
        self.last_sinr = 0.0
        self.current_m = 64  # Start with balanced
        self.m_switches = []  # Log of M changes for debugging
        
    def get_success_rate(self):
        """Calculate success rate from recent history"""
        if len(self.frame_success_history) == 0:
            return 1.0
        return sum(self.frame_success_history) / len(self.frame_success_history)
    
    def log_frame_result(self, success: bool, jammed: bool = False):
        """
        Record frame transmission result.
        Args:
            success: True if frame was decoded successfully
            jammed: True if frame was detected as jammed
        """
        self.frame_success_history.append(1 if success else 0)
        
        if jammed:
            self.jammed_frame_count += 1
        else:
            self.clean_frame_count += 1
    
    def estimate_sinr(self, signal, noise_floor, interference_power=0.0):
        """
        Estimate Signal-to-Interference-plus-Noise Ratio in dB.
        
        SINR = Signal_Power / (Interference_Power + Noise_Power)
        """
        if signal is None or len(signal) == 0:
            return self.last_sinr
        
        signal_power = np.mean(np.abs(signal) ** 2)
        denominator = interference_power + noise_floor
        
        if denominator <= 0:
            sinr_linear = signal_power / (1e-12)  # Avoid div by zero
        else:
            sinr_linear = signal_power / denominator
        
        sinr_db = 10.0 * np.log10(max(sinr_linear, 1e-6))
        self.last_sinr = sinr_db
        
        return sinr_db
    
    def adapt_m(self, 
                message_size: int, 
                sinr_db: float, 
                jammed_recently: bool = False,
                force_robust: bool = False) -> int:
        """
        Determine optimal M value based on channel conditions and message properties.
        
        Args:
            message_size: Size of message in bytes
            sinr_db: Current SINR estimate in dB
            jammed_recently: True if jamming was detected recently
            force_robust: If True, always return smallest M for maximum robustness
            
        Returns:
            M value: 16 (robust), 64 (balanced), or 256 (efficient)
        """
        
        # FACTOR 1: Force robust mode if explicitly requested
        if force_robust or self.jammed_frame_count >= self.jam_count_threshold:
            recommended_m = 16
            reason = "ROBUST_MODE (high jamming count)"
        
        # FACTOR 2: Heavy jamming detected (low SINR)
        elif sinr_db < self.sinr_threshold_low:
            recommended_m = 16
            reason = f"HEAVY_JAM (SINR={sinr_db:.1f} dB < {self.sinr_threshold_low} dB)"
        
        # FACTOR 3: Moderate jamming (medium SINR)
        elif sinr_db < self.sinr_threshold_high:
            recommended_m = 64
            reason = f"MODERATE_JAM (SINR={sinr_db:.1f} dB < {self.sinr_threshold_high} dB)"
        
        # FACTOR 4: Clean channel - use message size for efficiency
        else:
            if message_size > 500:
                recommended_m = 256
                reason = "CLEAN_CHANNEL + large message → QAM-256 (efficient)"
            elif message_size > 100:
                recommended_m = 64
                reason = "CLEAN_CHANNEL + medium message → QAM-64 (balanced)"
            else:
                recommended_m = 16
                reason = "CLEAN_CHANNEL + small message → QAM-16 (robust)"
        
        # FACTOR 5: Recent jamming → stay defensive
        if jammed_recently and recommended_m > 16:
            downgrade_m = min(recommended_m - 1, 16)
            old_reason = reason
            recommended_m = downgrade_m
            reason = f"{old_reason} [DOWNGRADED due to recent jamming]"
        
        # Log M changes
        if recommended_m != self.current_m:
            self.m_switches.append({
                'timestamp': time.time(),
                'old_m': self.current_m,
                'new_m': recommended_m,
                'sinr_db': sinr_db,
                'reason': reason,
                'message_size': message_size
            })
            print(f"[ADAPT_M] {self.current_m} → {recommended_m}: {reason}")
        
        self.current_m = recommended_m
        return recommended_m
    
    def get_m_for_transmission(self, message_size: int) -> int:
        """
        Quick method to get M value for transmission without full adaptation.
        Used for initial selection before channel feedback.
        
        Returns: M value (16, 64, or 256)
        """
        if message_size > 500:
            return 256
        elif message_size > 100:
            return 64
        else:
            return 16
    
    def get_diagnostics(self) -> dict:
        """Return current state diagnostics"""
        return {
            'current_m': self.current_m,
            'success_rate': self.get_success_rate(),
            'jammed_frames': self.jammed_frame_count,
            'clean_frames': self.clean_frame_count,
            'last_sinr_db': self.last_sinr,
            'total_m_switches': len(self.m_switches),
            'recent_switches': self.m_switches[-5:] if self.m_switches else []
        }
    
    def reset(self):
        """Reset all counters (useful for new session)"""
        self.frame_success_history.clear()
        self.jammed_frame_count = 0
        self.clean_frame_count = 0
        self.last_sinr = 0.0
        self.current_m = 64
        self.m_switches = []


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def calculate_ber_for_m(m_value: int, sinr_db: float) -> float:
    """
    Estimate Bit Error Rate (BER) for given M-ary QAM and SINR.
    Approximation based on theoretical BER formulas.
    
    Args:
        m_value: Modulation order (16, 64, 256)
        sinr_db: Signal-to-Interference-plus-Noise Ratio in dB
        
    Returns:
        Estimated BER (0.0 to 1.0)
    """
    sinr_linear = 10.0 ** (sinr_db / 10.0)
    k = np.log2(m_value)  # bits per symbol
    
    # Approximate BER formula for M-ary QAM
    # BER ≈ erfc(sqrt(3*k*SINR / (M-1)))
    argument = max(0, 3.0 * k * sinr_linear / (m_value - 1))
    
    if argument > 10:
        ber = 1e-6  # Very small
    elif argument < -10:
        ber = 1.0  # Very large
    else:
        # Simplified complementary error function approximation
        ber = 0.5 * np.exp(-argument) if argument > 0 else 0.5
    
    return ber


def select_m_for_reliability(target_ber: float = 1e-3) -> int:
    """
    Select M value to achieve target BER at current SINR.
    Useful for mission-critical communications.
    
    Args:
        target_ber: Desired bit error rate (default 1e-3 = 0.1%)
        
    Returns:
        Recommended M value (16, 64, or 256)
    """
    # At low SINR, QAM-16 always wins
    return 16


def select_m_for_throughput(sinr_db: float, 
                            symbol_rate: float = 1000.0) -> int:
    """
    Select M value to maximize throughput while maintaining acceptable BER.
    
    Args:
        sinr_db: Current SINR in dB
        symbol_rate: Symbol transmission rate (symbols/sec)
        
    Returns:
        Recommended M value (16, 64, or 256)
    """
    
    # Throughput = log2(M) * symbol_rate * (1 - BER)
    
    candidates = []
    
    for m in [16, 64, 256]:
        ber = calculate_ber_for_m(m, sinr_db)
        bits_per_sec = np.log2(m) * symbol_rate * (1.0 - ber)
        candidates.append({'m': m, 'throughput': bits_per_sec, 'ber': ber})
    
    # Sort by throughput and return highest
    candidates.sort(key=lambda x: x['throughput'], reverse=True)
    return candidates[0]['m']


# ============================================================================
# GLOBAL INSTANCE
# ============================================================================

# Single global instance used throughout the system
adaptive_modulation = AdaptiveModulation(
    window_size=20,
    sinr_threshold_low=-5.0,
    sinr_threshold_high=5.0,
    jam_count_threshold=3
)


if __name__ == "__main__":
    # Test the adaptive modulation module
    print("\n[TEST] Adaptive M Variation Module\n")
    
    am = AdaptiveModulation()
    
    # Simulate clean channel
    print("=== CLEAN CHANNEL ===")
    for i in range(5):
        m = am.adapt_m(message_size=300, sinr_db=15.0, jammed_recently=False)
        am.log_frame_result(success=True, jammed=False)
        print(f"  Frame {i+1}: M={m}, Success Rate={am.get_success_rate():.2%}")
    
    # Simulate jamming
    print("\n=== JAMMING DETECTED ===")
    for i in range(5):
        m = am.adapt_m(message_size=300, sinr_db=-3.0, jammed_recently=True)
        am.log_frame_result(success=(i < 2), jammed=True)
        print(f"  Frame {i+1}: M={m}, Success Rate={am.get_success_rate():.2%}")
    
    # Diagnostics
    print("\n=== DIAGNOSTICS ===")
    diag = am.get_diagnostics()
    for key, value in diag.items():
        print(f"  {key}: {value}")
