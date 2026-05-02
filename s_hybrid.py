# s_hybrid.py
"""
ML-Hybrid Sender (Branch: rayan-implementation)

Sender with integrated hybrid threat assessment for adaptive transmission:
  - Assesses spectrum before transmission using hybrid_anti_jamming_manager
  - Selects modulation based on unified ML threat score
  - Detects jamming early to prevent transmission failures
  - Logs transmission decisions with threat assessment
"""

import os
import sys
import json
import time
import numpy as np
from typing import Optional, Dict, Tuple
from datetime import datetime
import warnings

warnings.filterwarnings('ignore', category=UserWarning)

try:
    from Crypto.Cipher import AES
    from Crypto.Random import get_random_bytes
except ImportError:
    print("ERROR: pycryptodome not installed")
    sys.exit(1)

try:
    from reedsolo import RSCodec
except ImportError:
    print("ERROR: reedsolo not installed")
    sys.exit(1)

# Import hybrid components
from hybrid_anti_jamming_manager import hybrid_manager, ThreatLevel
from adaptive_m_variation import adaptive_modulation


# Configuration
OFDM_NUM_SUBCARRIERS = 256
OFDM_CYCLIC_PREFIX = 64
ENCRYPTION_KEY = b'5G-5G-5G-5G-5G-5G-5G-5G-5G-5G-5G-5G-5G-5G-5G-5G'
RS_NSYM = 40
rs_codec = RSCodec(RS_NSYM)

# Metrics
TRANSMISSION_STATS = {
    'total_transmissions': 0,
    'successful_transmissions': 0,
    'blocked_transmissions': 0,
    'robust_transmissions': 0,
    'assessment_count': 0,
    'threat_level_histogram': {'NONE': 0, 'LOW': 0, 'MEDIUM': 0, 'HIGH': 0, 'CRITICAL': 0},
    'modulation_histogram': {16: 0, 64: 0, 256: 0},
    'transmission_events': []
}


def assess_channel_hybrid(sensing_signal: Optional[np.ndarray] = None, sensing_energy: Optional[float] = None) -> Tuple[str, int, Dict]:
    """
    Assess channel using hybrid threat assessment before transmission.
    
    Returns:
        (action, recommended_m, assessment_dict)
    """
    
    # Generate random sensing signal if not provided
    if sensing_signal is None:
        sensing_signal = np.random.normal(0, 0.1, size=512) + 1j * np.random.normal(0, 0.1, size=512)
    
    # Get hybrid assessment
    assessment = hybrid_manager.assess_packet(
        ofdm_signal=sensing_signal,
        sensing_energy=sensing_energy or np.mean(np.abs(sensing_signal) ** 2),
        scan_type=1.0
    )
    
    TRANSMISSION_STATS['assessment_count'] += 1
    TRANSMISSION_STATS['threat_level_histogram'][assessment.unified_threat_level.name] += 1
    TRANSMISSION_STATS['modulation_histogram'][assessment.recommended_m] += 1
    
    assessment_dict = {
        'timestamp': datetime.now().isoformat(),
        'ml_threat_prob': float(assessment.ml_threat_probability),
        'unified_threat_level': assessment.unified_threat_level.name,
        'spectrum_state': assessment.spectrum_state,
        'interference_type': assessment.interference_type,
        'agreement_score': float(assessment.agreement_score),
        'recommended_m': assessment.recommended_m,
        'recommended_action': assessment.recommended_action
    }
    
    return assessment.recommended_action, assessment.recommended_m, assessment_dict


def encrypt_message(message: str, key: bytes) -> bytes:
    """Encrypt message with AES-GCM"""
    plaintext = message.encode('utf-8')
    cipher = AES.new(key, AES.MODE_GCM)
    nonce = cipher.nonce
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return nonce + tag + ciphertext


def encode_frame(data: bytes, m: int) -> Tuple[bytes, np.ndarray]:
    """Encode frame with error correction and generate OFDM signal"""
    
    # Add error correction
    try:
        encoded = rs_codec.encode(data)
    except:
        encoded = data
    
    # Generate OFDM signal
    message_bits = np.unpackbits(np.frombuffer(encoded[:12], dtype=np.uint8))
    
    # Convert bits to QAM symbols
    symbols_per_subcarrier = int(np.log2(m))
    num_symbols = len(message_bits) // symbols_per_subcarrier
    
    subcarrier_data = []
    for i in range(num_symbols):
        symbol_bits = message_bits[i*symbols_per_subcarrier:(i+1)*symbols_per_subcarrier]
        symbol_idx = int(''.join(map(str, symbol_bits)), 2) % m
        magnitude = np.sqrt(symbol_idx + 1) / np.sqrt(m)
        phase = 2 * np.pi * symbol_idx / m
        symbol = magnitude * np.exp(1j * phase)
        subcarrier_data.append(symbol)
    
    # Pad to OFDM size
    while len(subcarrier_data) < OFDM_NUM_SUBCARRIERS:
        subcarrier_data.append(0.0)
    
    # IFFT for time-domain OFDM
    ofdm_signal = np.fft.ifft(np.array(subcarrier_data[:OFDM_NUM_SUBCARRIERS]))
    
    # Add cyclic prefix
    cyclic_prefix = ofdm_signal[-OFDM_CYCLIC_PREFIX:]
    ofdm_with_cp = np.concatenate([cyclic_prefix, ofdm_signal])
    
    return bytes(encoded[:96]), ofdm_with_cp


def send_message_hybrid(
    message: str,
    sender_id: str,
    receiver_id: str,
    force_transmission: bool = False
) -> Dict:
    """
    Send message with hybrid threat assessment.
    
    Returns:
        transmission_result_dict
    """
    
    TRANSMISSION_STATS['total_transmissions'] += 1
    
    # Step 1: Assess channel threat
    action, recommended_m, assessment = assess_channel_hybrid()
    
    # Step 2: Determine if we should transmit
    should_transmit = (action == "TRANSMIT") or force_transmission
    use_robust = (action == "ROBUST")
    
    if not should_transmit and not use_robust:
        TRANSMISSION_STATS['blocked_transmissions'] += 1
        result = {
            'status': 'BLOCKED',
            'reason': f"Channel threat level: {assessment['unified_threat_level']}",
            'sender': sender_id,
            'receiver': receiver_id,
            'message': message,
            'assessment': assessment
        }
        TRANSMISSION_STATS['transmission_events'].append(result)
        return result
    
    # Step 3: Select modulation
    if use_robust:
        TRANSMISSION_STATS['robust_transmissions'] += 1
        m = 16  # Force robust modulation
    else:
        m = recommended_m
    
    # Step 4: Encrypt message
    encrypted = encrypt_message(message, ENCRYPTION_KEY)
    
    # Step 5: Encode and generate OFDM
    frame, ofdm_signal = encode_frame(encrypted, m)
    
    # Step 6: Log successful transmission
    TRANSMISSION_STATS['successful_transmissions'] += 1
    
    result = {
        'status': 'TRANSMITTED',
        'sender': sender_id,
        'receiver': receiver_id,
        'message': message,
        'modulation_used': m,
        'transmission_type': 'ROBUST' if use_robust else 'STANDARD',
        'frame_size': len(frame),
        'assessment': assessment,
        'timestamp': datetime.now().isoformat()
    }
    
    TRANSMISSION_STATS['transmission_events'].append(result)
    return result


def export_sender_hybrid_results():
    """Export sender transmission statistics"""
    
    os.makedirs('results', exist_ok=True)
    
    # Calculate statistics
    total = TRANSMISSION_STATS['total_transmissions']
    success = TRANSMISSION_STATS['successful_transmissions']
    blocked = TRANSMISSION_STATS['blocked_transmissions']
    robust = TRANSMISSION_STATS['robust_transmissions']
    
    success_rate = (success / max(total, 1)) * 100
    blocked_rate = (blocked / max(total, 1)) * 100
    robust_rate = (robust / max(success, 1)) * 100 if success > 0 else 0
    
    results = {
        'implementation': 'ML-Hybrid Sender (rayan-implementation)',
        'metrics': {
            'total_transmissions': total,
            'successful_transmissions': success,
            'transmission_success_rate_percent': round(success_rate, 2),
            'blocked_transmissions': blocked,
            'blocked_rate_percent': round(blocked_rate, 2),
            'robust_transmissions': robust,
            'robust_transmission_rate_percent': round(robust_rate, 2),
            'total_assessments': TRANSMISSION_STATS['assessment_count']
        },
        'threat_level_distribution': TRANSMISSION_STATS['threat_level_histogram'],
        'modulation_distribution': TRANSMISSION_STATS['modulation_histogram'],
        'recent_events': TRANSMISSION_STATS['transmission_events'][-20:]
    }
    
    with open('results/s_hybrid_results.json', 'w') as f:
        json.dump(results, f, indent=2)
    
    print("\n[SENDER] Exported results to s_hybrid_results.json")
    print(f"  Success Rate: {success_rate:.1f}%")
    print(f"  Robust Transmissions: {robust_rate:.1f}%")
    
    return results


def main():
    """Simulate hybrid sender transmitting messages"""
    
    print("\n" + "="*70)
    print("ML-HYBRID SENDER (rayan-implementation)")
    print("Adaptive Transmission with Hybrid Threat Assessment")
    print("="*70 + "\n")
    
    messages = [
        "5G Network Status: Normal",
        "Alert: Jamming Detected",
        "Critical: Signal Degradation",
        "System: Running Adaptive Protocol",
        "Network: Establishing Secure Channel"
    ]
    
    for i in range(10):
        message = messages[i % len(messages)]
        sender = f"S{i % 3 + 1}"
        receiver = "BS1"
        
        result = send_message_hybrid(message, sender, receiver)
        
        status_icon = "✓" if result['status'] == "TRANSMITTED" else "✗"
        print(f"[{status_icon}] {sender} → {receiver}: {message[:30]:30s} | "
              f"Threat: {result['assessment']['unified_threat_level']:8s} | "
              f"M: {result.get('modulation_used', 'N/A'):3}")
    
    # Export results
    export_sender_hybrid_results()


if __name__ == "__main__":
    main()
