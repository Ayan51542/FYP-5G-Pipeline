# r_hybrid.py
"""
ML-Hybrid Receiver (Branch: rayan-implementation)

Receiver with comprehensive ML-hybrid threat analysis and visualization:
  - Analyzes received frames with hybrid threat assessment
  - Tracks decryption failures with ML threat correlation
  - Generates threat correlation plots
  - Exports comprehensive diagnostic data
"""

import os
import sys
import json
import numpy as np
import matplotlib
import matplotlib.pyplot as plt
from typing import Dict, List, Optional
from datetime import datetime
from collections import deque
import warnings

warnings.filterwarnings('ignore', category=UserWarning)
matplotlib.use('Agg')

try:
    from Crypto.Cipher import AES
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


# Configuration
ENCRYPTION_KEY = b'5G-5G-5G-5G-5G-5G-5G-5G-5G-5G-5G-5G-5G-5G-5G-5G'
RS_NSYM = 40
rs_codec = RSCodec(RS_NSYM)

# Receiver statistics
RECEIVER_STATS = {
    'total_frames_received': 0,
    'successfully_decoded': 0,
    'decryption_failures': 0,
    'error_corrected': 0,
    'frames_in_jamming': 0,
    'threat_correlation_data': [],
    'decryption_failure_correlations': [],
    'threat_level_at_failure': {'NONE': 0, 'LOW': 0, 'MEDIUM': 0, 'HIGH': 0, 'CRITICAL': 0}
}


def decrypt_data(encrypted_data: bytes, key: bytes) -> Optional[str]:
    """Decrypt AES-GCM encrypted data"""
    try:
        nonce = encrypted_data[:16]
        tag = encrypted_data[16:32]
        ciphertext = encrypted_data[32:]
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
        return plaintext.decode('utf-8')
    except:
        return None


def decode_frame(frame_bytes: bytes) -> Optional[bytes]:
    """Decode frame with error correction"""
    try:
        message_bytes, decoded = rs_codec.decode(frame_bytes)
        return bytes(message_bytes)
    except:
        return None


def receive_and_analyze_hybrid(
    frame: bytes,
    ofdm_signal: np.ndarray,
    sender_id: str
) -> Dict:
    """
    Receive and analyze frame with hybrid threat assessment.
    
    Returns:
        analysis_result_dict
    """
    
    RECEIVER_STATS['total_frames_received'] += 1
    
    # Step 1: Hybrid threat assessment
    assessment = hybrid_manager.assess_packet(
        ofdm_signal=ofdm_signal,
        sensing_energy=np.mean(np.abs(ofdm_signal) ** 2),
        scan_type=1.0
    )
    
    # Step 2: Attempt decode
    decoded_data = decode_frame(frame)
    decode_success = decoded_data is not None
    
    if decode_success:
        RECEIVER_STATS['successfully_decoded'] += 1
        RECEIVER_STATS['error_corrected'] += 1
    
    # Step 3: Attempt decrypt
    decrypted_msg = None
    if decoded_data:
        decrypted_msg = decrypt_data(decoded_data, ENCRYPTION_KEY)
        if decrypted_msg is None:
            RECEIVER_STATS['decryption_failures'] += 1
    
    # Step 4: Record threat correlation
    threat_record = {
        'frame_number': RECEIVER_STATS['total_frames_received'],
        'sender': sender_id,
        'timestamp': datetime.now().isoformat(),
        'ml_threat_prob': float(assessment.ml_threat_probability),
        'unified_threat_level': assessment.unified_threat_level.name,
        'confidence': float(assessment.unified_confidence),
        'spectrum_state': assessment.spectrum_state,
        'agreement_score': float(assessment.agreement_score),
        'decode_success': decode_success,
        'decryption_success': decrypted_msg is not None,
        'message_recovered': decrypted_msg or "FAILED"
    }
    
    RECEIVER_STATS['threat_correlation_data'].append(threat_record)
    
    # Track threat level at failures
    if decrypted_msg is None:
        RECEIVER_STATS['decryption_failure_correlations'].append(threat_record)
        RECEIVER_STATS['threat_level_at_failure'][assessment.unified_threat_level.name] += 1
    
    # Track frames in jamming
    if assessment.unified_threat_level in [ThreatLevel.HIGH, ThreatLevel.CRITICAL]:
        RECEIVER_STATS['frames_in_jamming'] += 1
    
    return threat_record


def plot_threat_vs_decryption_correlation():
    """Plot correlation between threat probability and decryption success"""
    
    threat_data = RECEIVER_STATS['threat_correlation_data']
    if not threat_data:
        return
    
    threat_probs = [t['ml_threat_prob'] for t in threat_data]
    success_flags = [1.0 if t['decryption_success'] else 0.0 for t in threat_data]
    confidence_scores = [t['confidence'] for t in threat_data]
    agreement_scores = [t['agreement_score'] for t in threat_data]
    
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle('ML-Hybrid Threat vs Decryption Success Correlation', fontsize=14, fontweight='bold')
    
    # Plot 1: Threat Probability vs Success
    ax = axes[0, 0]
    colors = ['green' if s else 'red' for s in success_flags]
    ax.scatter(threat_probs, success_flags, c=colors, alpha=0.6, s=60)
    ax.set_xlabel('ML Threat Probability', fontsize=11)
    ax.set_ylabel('Decryption Success (1=Yes, 0=No)', fontsize=11)
    ax.set_title('Threat vs Decryption Success')
    ax.set_ylim([-0.1, 1.1])
    ax.grid(True, alpha=0.3)
    
    # Plot 2: Unified Confidence Distribution
    ax = axes[0, 1]
    ax.hist(confidence_scores, bins=20, color='blue', alpha=0.7, edgecolor='black')
    ax.set_xlabel('Unified Confidence Score', fontsize=11)
    ax.set_ylabel('Frequency', fontsize=11)
    ax.set_title('Confidence Score Distribution')
    ax.grid(True, alpha=0.3, axis='y')
    
    # Plot 3: Agreement Score Distribution
    ax = axes[1, 0]
    ax.hist(agreement_scores, bins=20, color='green', alpha=0.7, edgecolor='black')
    ax.set_xlabel('Detector Agreement Score', fontsize=11)
    ax.set_ylabel('Frequency', fontsize=11)
    ax.set_title('Agreement Score Distribution')
    ax.grid(True, alpha=0.3, axis='y')
    
    # Plot 4: Threat Level at Failures
    ax = axes[1, 1]
    threat_levels = list(RECEIVER_STATS['threat_level_at_failure'].keys())
    failure_counts = list(RECEIVER_STATS['threat_level_at_failure'].values())
    colors_bar = ['green', 'yellow', 'orange', 'red', 'darkred']
    bars = ax.bar(threat_levels, failure_counts, color=colors_bar, alpha=0.7, edgecolor='black')
    ax.set_xlabel('Threat Level', fontsize=11)
    ax.set_ylabel('Decryption Failures', fontsize=11)
    ax.set_title('Failures by Threat Level')
    ax.grid(True, alpha=0.3, axis='y')
    
    plt.tight_layout()
    plt.savefig('results/threat_vs_decryption_correlation.png', dpi=150, bbox_inches='tight')
    print("[PLOT] Saved: threat_vs_decryption_correlation.png")
    plt.close()


def plot_receiver_performance_metrics():
    """Plot receiver performance metrics"""
    
    threat_data = RECEIVER_STATS['threat_correlation_data']
    if not threat_data:
        return
    
    total = RECEIVER_STATS['total_frames_received']
    success = RECEIVER_STATS['successfully_decoded']
    failures = RECEIVER_STATS['decryption_failures']
    jamming_frames = RECEIVER_STATS['frames_in_jamming']
    
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle('ML-Hybrid Receiver Performance Metrics', fontsize=14, fontweight='bold')
    
    # Plot 1: Frame success/failure breakdown
    ax = axes[0]
    categories = ['Successful', 'Decryption Failed', 'Jamming Detected', 'Other']
    values = [success - failures, failures, jamming_frames, total - success - jamming_frames]
    colors_pie = ['green', 'red', 'orange', 'gray']
    
    wedges, texts, autotexts = ax.pie(
        values, labels=categories, autopct='%1.1f%%',
        colors=colors_pie, startangle=90, textprops={'fontsize': 10}
    )
    ax.set_title('Frame Outcome Distribution')
    
    # Plot 2: Key metrics
    ax = axes[1]
    ax.axis('off')
    
    success_rate = (success / max(total, 1)) * 100
    failure_rate = (failures / max(total, 1)) * 100
    jamming_rate = (jamming_frames / max(total, 1)) * 100
    
    metrics_text = f"""
    RECEIVER PERFORMANCE METRICS
    ══════════════════════════════════════
    
    Total Frames Received:        {total}
    Successfully Decoded:         {success}
    Decryption Failures:          {failures}
    Success Rate:                 {success_rate:.1f}%
    Failure Rate:                 {failure_rate:.1f}%
    
    Frames in Jamming Zone:       {jamming_frames}
    Jamming Detection Rate:       {jamming_rate:.1f}%
    
    Error-Corrected Frames:       {RECEIVER_STATS['error_corrected']}
    Correction Success Rate:      {(RECEIVER_STATS['error_corrected'] / max(success, 1) * 100):.1f}%
    
    ══════════════════════════════════════
    Threat Level at Failures:
      NONE:     {RECEIVER_STATS['threat_level_at_failure']['NONE']}
      LOW:      {RECEIVER_STATS['threat_level_at_failure']['LOW']}
      MEDIUM:   {RECEIVER_STATS['threat_level_at_failure']['MEDIUM']}
      HIGH:     {RECEIVER_STATS['threat_level_at_failure']['HIGH']}
      CRITICAL: {RECEIVER_STATS['threat_level_at_failure']['CRITICAL']}
    """
    
    ax.text(0.1, 0.5, metrics_text, fontsize=10, family='monospace',
            verticalalignment='center', bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))
    
    plt.tight_layout()
    plt.savefig('results/receiver_performance_metrics.png', dpi=150, bbox_inches='tight')
    print("[PLOT] Saved: receiver_performance_metrics.png")
    plt.close()


def export_receiver_hybrid_results():
    """Export comprehensive receiver results"""
    
    os.makedirs('results', exist_ok=True)
    
    total = RECEIVER_STATS['total_frames_received']
    success = RECEIVER_STATS['successfully_decoded']
    failures = RECEIVER_STATS['decryption_failures']
    jamming = RECEIVER_STATS['frames_in_jamming']
    
    results = {
        'implementation': 'ML-Hybrid Receiver (rayan-implementation)',
        'metrics': {
            'total_frames_received': total,
            'successfully_decoded': success,
            'decode_success_rate_percent': round((success / max(total, 1)) * 100, 2),
            'decryption_failures': failures,
            'failure_rate_percent': round((failures / max(total, 1)) * 100, 2),
            'frames_in_jamming_zone': jamming,
            'jamming_detection_rate_percent': round((jamming / max(total, 1)) * 100, 2),
            'error_corrected_frames': RECEIVER_STATS['error_corrected']
        },
        'threat_level_correlation': RECEIVER_STATS['threat_level_at_failure'],
        'correlation_events': len(RECEIVER_STATS['threat_correlation_data']),
        'failure_correlations': len(RECEIVER_STATS['decryption_failure_correlations'])
    }
    
    with open('results/r_hybrid_results.json', 'w') as f:
        json.dump(results, f, indent=2)
    print("[RECEIVER] Exported results to r_hybrid_results.json")
    
    with open('results/r_hybrid_threat_correlation.json', 'w') as f:
        json.dump(RECEIVER_STATS['threat_correlation_data'], f, indent=2)
    print("[RECEIVER] Exported threat correlations")
    
    # Generate visualizations
    plot_threat_vs_decryption_correlation()
    plot_receiver_performance_metrics()
    
    return results


def main():
    """Simulate hybrid receiver processing frames"""
    
    print("\n" + "="*70)
    print("ML-HYBRID RECEIVER (rayan-implementation)")
    print("Decryption with Threat Correlation Analysis")
    print("="*70 + "\n")
    
    # Simulate receiving frames with varying threat levels
    num_frames = 50
    jamming_probability = 0.3
    
    for frame_idx in range(num_frames):
        # Generate random frame
        frame = np.random.randint(0, 256, size=96, dtype=np.uint8).tobytes()
        
        # Generate OFDM signal
        ofdm_signal = np.random.normal(0, 0.1, size=512) + 1j * np.random.normal(0, 0.1, size=512)
        
        # Simulate jamming
        if np.random.random() < jamming_probability:
            ofdm_signal = ofdm_signal + 0.3 * (np.random.normal(0, 1, size=512) + 1j * np.random.normal(0, 1, size=512))
        
        # Receive and analyze
        analysis = receive_and_analyze_hybrid(
            frame=frame,
            ofdm_signal=ofdm_signal,
            sender_id=f"S{frame_idx % 3 + 1}"
        )
        
        # Print status
        if frame_idx % 10 == 0:
            status = "✓" if analysis['decryption_success'] else "✗"
            print(f"[Frame {frame_idx+1:2d}] {status} Threat: {analysis['unified_threat_level']:8s} "
                  f"Confidence: {analysis['confidence']:.3f} "
                  f"Agreement: {analysis['agreement_score']:.3f}")
    
    # Export results
    print("\n[INFO] Exporting receiver results...")
    results = export_receiver_hybrid_results()
    
    print("\n" + "="*70)
    print("RECEIVER RESULTS SUMMARY")
    print("="*70)
    print(f"Total Frames:              {results['metrics']['total_frames_received']}")
    print(f"Successfully Decoded:      {results['metrics']['successfully_decoded']}")
    print(f"Success Rate:              {results['metrics']['decode_success_rate_percent']:.1f}%")
    print(f"Decryption Failures:       {results['metrics']['decryption_failures']}")
    print(f"Frames in Jamming:         {results['metrics']['frames_in_jamming_zone']}")
    print("="*70 + "\n")


if __name__ == "__main__":
    main()
