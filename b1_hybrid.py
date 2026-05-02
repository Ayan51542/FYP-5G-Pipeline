# b1_hybrid.py
"""
ML-Hybrid Base Station (Branch: rayan-implementation)

Combines all 3 anti-jamming techniques through hybrid_anti_jamming_manager:
  1. Adaptive M Variation (via threat_model_runtime threat score)
  2. Enhanced Spectrum Sensing (integrated with ML)
  3. Intelligent Jammer Detector (feature-based ML)
  
Uses ensemble ML threat prediction as central decision-making layer.
"""

import os
import sys
import json
import threading
import time
import signal
import numpy as np
from typing import Dict, Tuple, Optional, List
from enum import Enum
from dataclasses import dataclass, asdict
from datetime import datetime
import base64
import matplotlib
import matplotlib.pyplot as plt
import warnings

warnings.filterwarnings('ignore', category=UserWarning)
matplotlib.use('Agg')  # Non-interactive backend

try:
    from Crypto.Cipher import AES
    from Crypto.Random import get_random_bytes
    from Crypto.Util.Padding import pad, unpad
except ImportError:
    print("ERROR: pycryptodome not installed. Install with: pip install pycryptodome")
    sys.exit(1)

try:
    from reedsolo import RSCodec
except ImportError:
    print("ERROR: reedsolo not installed. Install with: pip install reedsolo")
    sys.exit(1)

# Import all components
from hybrid_anti_jamming_manager import hybrid_manager, HybridThreatAssessment, ThreatLevel
from adaptive_m_variation import adaptive_modulation
from enhanced_spectrum_sensing import spectrum_sensor
from intelligent_jammer_detector import jammer_detector


# Global OFDM Config
OFDM_NUM_SUBCARRIERS = 256
OFDM_CYCLIC_PREFIX = 64
MODULATION_SIZES = [16, 64, 256]  # QAM
FRAME_SIZE = 96  # bits

# Encryption
ENCRYPTION_KEY = b'5G-5G-5G-5G-5G-5G-5G-5G-5G-5G-5G-5G-5G-5G-5G-5G'  # 16 bytes for AES-128

# Error Correction
RS_NSYM = 40
rs_codec = RSCodec(RS_NSYM)


@dataclass
class MLHybridMetrics:
    """Track metrics for ML-hybrid approach"""
    total_frames: int = 0
    successful_frames: int = 0
    jammed_frames: int = 0
    ml_scored_frames: int = 0
    ml_alerts: int = 0
    ml_critical_alerts: int = 0
    decryption_failures: int = 0
    error_corrected_frames: int = 0
    average_ml_threat: float = 0.0
    agreement_score: float = 0.0
    modulation_histogram: Dict[int, int] = None
    threat_level_histogram: Dict[str, int] = None
    
    def __post_init__(self):
        if self.modulation_histogram is None:
            self.modulation_histogram = {16: 0, 64: 0, 256: 0}
        if self.threat_level_histogram is None:
            self.threat_level_histogram = {
                "NONE": 0, "LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0
            }


# Global state
METRICS = MLHybridMetrics()
THREAT_HISTORY = []
METRICS_LOCK = threading.Lock()
SHUTDOWN_EVENT = threading.Event()


class FrameDecoder:
    """Decode received frames with error correction"""
    
    @staticmethod
    def decode(frame_bytes: bytes) -> Optional[bytes]:
        """Decode with RS error correction"""
        try:
            message_bytes, decoded = rs_codec.decode(frame_bytes)
            return bytes(message_bytes)
        except Exception as e:
            return None
    
    @staticmethod
    def encrypt_data(plaintext: str, key: bytes) -> bytes:
        """AES-GCM encryption"""
        plaintext_bytes = plaintext.encode('utf-8')
        cipher = AES.new(key, AES.MODE_GCM)
        nonce = cipher.nonce
        ciphertext, tag = cipher.encrypt_and_digest(plaintext_bytes)
        return nonce + tag + ciphertext
    
    @staticmethod
    def decrypt_data(encrypted_data: bytes, key: bytes) -> Optional[str]:
        """AES-GCM decryption"""
        try:
            nonce = encrypted_data[:16]
            tag = encrypted_data[16:32]
            ciphertext = encrypted_data[32:]
            cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
            plaintext_bytes = cipher.decrypt_and_verify(ciphertext, tag)
            return plaintext_bytes.decode('utf-8')
        except Exception as e:
            return None


def generate_ofdm_symbols(message_bits: np.ndarray, m: int) -> np.ndarray:
    """Generate OFDM symbols from message bits"""
    symbols_per_subcarrier = int(np.log2(m))
    num_symbols = len(message_bits) // symbols_per_subcarrier
    
    subcarrier_data = []
    for i in range(num_symbols):
        symbol_bits = message_bits[i*symbols_per_subcarrier:(i+1)*symbols_per_subcarrier]
        if len(symbol_bits) < symbols_per_subcarrier:
            symbol_bits = np.pad(symbol_bits, (0, symbols_per_subcarrier-len(symbol_bits)))
        
        symbol_idx = int(''.join(map(str, symbol_bits)), 2) % m
        magnitude = np.sqrt(symbol_idx + 1) / np.sqrt(m)
        phase = 2 * np.pi * symbol_idx / m
        symbol = magnitude * np.exp(1j * phase)
        subcarrier_data.append(symbol)
    
    # Pad to OFDM_NUM_SUBCARRIERS
    while len(subcarrier_data) < OFDM_NUM_SUBCARRIERS:
        subcarrier_data.append(0.0)
    
    # IFFT to get time-domain OFDM signal
    ofdm_signal = np.fft.ifft(np.array(subcarrier_data[:OFDM_NUM_SUBCARRIERS]))
    
    # Add cyclic prefix
    cyclic_prefix = ofdm_signal[-OFDM_CYCLIC_PREFIX:]
    ofdm_with_cp = np.concatenate([cyclic_prefix, ofdm_signal])
    
    return ofdm_with_cp


def simulate_jamming(signal: np.ndarray, jam_power: float = 0.5) -> np.ndarray:
    """Simulate jamming interference"""
    jamming_signal = np.random.normal(0, jam_power, size=signal.shape) + \
                     1j * np.random.normal(0, jam_power, size=signal.shape)
    return signal + jamming_signal


def process_incoming_frame_hybrid(
    frame_bytes: bytes,
    signal: np.ndarray,
    sender_id: str,
    ground_truth_jammed: bool = False,
) -> Tuple[Optional[str], Dict]:
    """
    Process frame with ML-hybrid threat assessment.
    
    Returns:
        (decoded_message, metadata_dict)
    """
    
    with METRICS_LOCK:
        METRICS.total_frames += 1
    
    # ============ STEP 1: ML-HYBRID THREAT ASSESSMENT ============
    assessment = hybrid_manager.assess_packet(
        ofdm_signal=signal,
        sensing_energy=np.mean(np.abs(signal) ** 2),
        signal_strength_db=None
    )
    
    with METRICS_LOCK:
        METRICS.ml_scored_frames += 1
        METRICS.average_ml_threat += assessment.ml_threat_probability
        METRICS.agreement_score += assessment.agreement_score
        METRICS.threat_level_histogram[assessment.unified_threat_level.name] += 1
        METRICS.modulation_histogram[assessment.recommended_m] += 1
    
    # Log threat
    threat_record = {
        'timestamp': datetime.now().isoformat(),
        'sender': sender_id,
        'ml_threat_prob': float(assessment.ml_threat_probability),
        'unified_threat_level': assessment.unified_threat_level.name,
        'confidence': float(assessment.unified_confidence),
        'spectrum_state': assessment.spectrum_state,
        'interference_type': assessment.interference_type,
        'agreement_score': float(assessment.agreement_score),
        'all_agree_jammed': assessment.all_agree_jammed,
        'recommended_m': assessment.recommended_m,
        'ground_truth_jammed': ground_truth_jammed
    }
    THREAT_HISTORY.append(threat_record)
    
    # Count alerts
    if assessment.unified_threat_level in [ThreatLevel.HIGH, ThreatLevel.CRITICAL]:
        with METRICS_LOCK:
            METRICS.ml_alerts += 1
            if assessment.unified_threat_level == ThreatLevel.CRITICAL:
                METRICS.ml_critical_alerts += 1
    
    # ============ STEP 2: DECODE FRAME ============
    try:
        decoded_data = FrameDecoder.decode(frame_bytes)
        if decoded_data:
            with METRICS_LOCK:
                METRICS.successful_frames += 1
                METRICS.error_corrected_frames += 1
            
            # Decrypt
            decrypted_msg = FrameDecoder.decrypt_data(decoded_data, ENCRYPTION_KEY)
            if decrypted_msg:
                return decrypted_msg, threat_record
            else:
                with METRICS_LOCK:
                    METRICS.decryption_failures += 1
                return None, threat_record
        else:
            with METRICS_LOCK:
                if assessment.unified_threat_level in [ThreatLevel.HIGH, ThreatLevel.CRITICAL]:
                    METRICS.jammed_frames += 1
            return None, threat_record
    
    except Exception as e:
        return None, threat_record


def plot_ml_hybrid_threat_timeline():
    """Create ML hybrid threat detection timeline plot"""
    
    if not THREAT_HISTORY:
        print("[PLOT] No threat history to plot")
        return
    
    timestamps = [i for i in range(len(THREAT_HISTORY))]
    ml_threats = [t['ml_threat_prob'] for t in THREAT_HISTORY]
    confidence = [t['confidence'] for t in THREAT_HISTORY]
    agreement = [t['agreement_score'] for t in THREAT_HISTORY]
    
    fig, axes = plt.subplots(3, 1, figsize=(14, 10))
    fig.suptitle('ML-Hybrid Threat Detection Timeline', fontsize=16, fontweight='bold')
    
    # Plot 1: ML Threat Probability
    ax = axes[0]
    ax.plot(timestamps, ml_threats, 'r-', linewidth=2, label='ML Threat Prob')
    ax.axhline(y=0.65, color='orange', linestyle='--', linewidth=2, label='Threat Threshold')
    ax.fill_between(timestamps, 0, ml_threats, alpha=0.3, color='red')
    ax.set_ylabel('Threat Probability', fontsize=11)
    ax.legend(loc='upper left')
    ax.grid(True, alpha=0.3)
    ax.set_ylim([0, 1])
    
    # Plot 2: Unified Confidence & Agreement
    ax = axes[1]
    ax.plot(timestamps, confidence, 'b-', linewidth=2, label='Unified Confidence', marker='o', markersize=3)
    ax.plot(timestamps, agreement, 'g--', linewidth=2, label='Agreement Score', marker='s', markersize=3)
    ax.set_ylabel('Score (0-1)', fontsize=11)
    ax.legend(loc='upper left')
    ax.grid(True, alpha=0.3)
    ax.set_ylim([0, 1])
    
    # Plot 3: Threat Levels
    ax = axes[2]
    threat_levels = [t['unified_threat_level'] for t in THREAT_HISTORY]
    threat_level_map = {'NONE': 0, 'LOW': 1, 'MEDIUM': 2, 'HIGH': 3, 'CRITICAL': 4}
    threat_values = [threat_level_map[tl] for tl in threat_levels]
    colors = ['green' if v == 0 else 'yellow' if v == 1 else 'orange' if v == 2 else 'red' if v == 3 else 'darkred' for v in threat_values]
    
    ax.scatter(timestamps, threat_values, c=colors, s=50, alpha=0.7)
    ax.set_ylabel('Threat Level', fontsize=11)
    ax.set_yticks([0, 1, 2, 3, 4])
    ax.set_yticklabels(['NONE', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'])
    ax.set_xlabel('Frame Number', fontsize=11)
    ax.grid(True, alpha=0.3, axis='y')
    
    plt.tight_layout()
    plt.savefig('results/ml_hybrid_threat_timeline.png', dpi=150, bbox_inches='tight')
    print("[PLOT] Saved: ml_hybrid_threat_timeline.png")
    plt.close()


def export_ml_hybrid_results():
    """Export comprehensive ML-hybrid results"""
    
    with METRICS_LOCK:
        # Calculate averages
        avg_ml_threat = METRICS.average_ml_threat / max(METRICS.ml_scored_frames, 1)
        avg_agreement = METRICS.agreement_score / max(METRICS.total_frames, 1)
        
        success_rate = METRICS.successful_frames / max(METRICS.total_frames, 1) * 100
        error_correction_rate = METRICS.error_corrected_frames / max(METRICS.successful_frames, 1) * 100 if METRICS.successful_frames > 0 else 0
        
        results = {
            'implementation': 'ML-Hybrid Anti-Jamming (rayan-implementation branch)',
            'techniques_combined': [
                'threat_model_runtime (RF/XGBoost ensemble)',
                'adaptive_m_variation',
                'enhanced_spectrum_sensing',
                'intelligent_jammer_detector'
            ],
            'metrics': {
                'total_frames_processed': METRICS.total_frames,
                'successful_frames': METRICS.successful_frames,
                'success_rate_percent': round(success_rate, 2),
                'jammed_frames_detected': METRICS.jammed_frames,
                'ml_scored_frames': METRICS.ml_scored_frames,
                'ml_alerts_triggered': METRICS.ml_alerts,
                'ml_critical_alerts': METRICS.ml_critical_alerts,
                'decryption_failures': METRICS.decryption_failures,
                'error_corrected_frames': METRICS.error_corrected_frames,
                'error_correction_rate_percent': round(error_correction_rate, 2),
                'average_ml_threat_probability': round(avg_ml_threat, 4),
                'average_detector_agreement': round(avg_agreement, 4),
            },
            'modulation_distribution': METRICS.modulation_histogram,
            'threat_level_distribution': METRICS.threat_level_histogram,
            'threat_detection_events': len(THREAT_HISTORY),
        }
        
        threat_history_json = THREAT_HISTORY.copy()
    
    # Save results
    os.makedirs('results', exist_ok=True)
    
    with open('results/ml_hybrid_results.json', 'w') as f:
        json.dump(results, f, indent=2)
    print("[EXPORT] Saved: ml_hybrid_results.json")
    
    with open('results/ml_hybrid_threat_history.json', 'w') as f:
        json.dump(threat_history_json, f, indent=2)
    print("[EXPORT] Saved: ml_hybrid_threat_history.json")
    
    # Create visualization
    plot_ml_hybrid_threat_timeline()
    
    return results


def main():
    """Simulate ML-hybrid base station processing"""
    
    print("\n" + "="*70)
    print("ML-HYBRID BASE STATION (rayan-implementation)")
    print("Combining: RF/XGBoost Ensemble + 3 Anti-Jamming Techniques")
    print("="*70 + "\n")
    
    # Simulate incoming frames with jamming
    num_frames = 50
    jam_probability = 0.3  # 30% of frames are jammed
    
    for frame_idx in range(num_frames):
        # Generate random message
        message = f"Test Frame {frame_idx + 1}"
        
        # Encrypt
        encrypted = FrameDecoder.encrypt_data(message, ENCRYPTION_KEY)
        
        # Add error correction
        try:
            encoded = rs_codec.encode(encrypted)
        except:
            encoded = encrypted
        
        # Generate OFDM signal with random M
        m = np.random.choice([16, 64, 256])
        message_bits = np.random.randint(0, 2, size=FRAME_SIZE)
        ofdm_signal = generate_ofdm_symbols(message_bits, m)
        
        # Simulate jamming
        is_jammed = np.random.random() < jam_probability
        if is_jammed:
            ofdm_signal = simulate_jamming(ofdm_signal, jam_power=0.3)
        
        # Process frame
        decoded, threat_info = process_incoming_frame_hybrid(
            frame_bytes=bytes(encoded[:96]),  # Truncate to frame size
            signal=ofdm_signal,
            sender_id=f"S{frame_idx % 3 + 1}",
            ground_truth_jammed=is_jammed
        )
        
        # Print status
        if frame_idx % 10 == 0:
            print(f"[Frame {frame_idx+1:3d}] Threat: {threat_info['unified_threat_level']:8s} "
                  f"Confidence: {threat_info['confidence']:.3f} "
                  f"Agreement: {threat_info['agreement_score']:.3f} "
                  f"M: {threat_info['recommended_m']}")
    
    # Export results
    print("\n[INFO] Exporting ML-hybrid results...")
    results = export_ml_hybrid_results()
    
    print("\n" + "="*70)
    print("ML-HYBRID RESULTS SUMMARY")
    print("="*70)
    print(f"Total Frames:              {results['metrics']['total_frames_processed']}")
    print(f"Successful:                {results['metrics']['successful_frames']}")
    print(f"Success Rate:              {results['metrics']['success_rate_percent']:.1f}%")
    print(f"ML Alerts:                 {results['metrics']['ml_alerts_triggered']}")
    print(f"Critical Alerts:           {results['metrics']['ml_critical_alerts']}")
    print(f"Avg Threat Probability:    {results['metrics']['average_ml_threat_probability']:.4f}")
    print(f"Avg Detector Agreement:    {results['metrics']['average_detector_agreement']:.4f}")
    print("="*70 + "\n")


if __name__ == "__main__":
    main()
