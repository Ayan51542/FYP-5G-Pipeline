# hybrid_anti_jamming_manager.py
"""
Hybrid Anti-Jamming Manager
Combines:
  1. threat_model_runtime.py (RF/XGBoost ensemble threat detection)
  2. adaptive_m_variation.py (Dynamic modulation selection)
  3. enhanced_spectrum_sensing.py (Spectrum monitoring)
  4. intelligent_jammer_detector.py (ML-based feature scoring)

Uses a unified threat scoring system to make adaptive decisions.
"""

import time
import numpy as np
import threading
from dataclasses import dataclass
from typing import Optional, Dict, Tuple
from collections import deque
from enum import Enum

from threat_model_runtime import ThreatModelRuntime
from adaptive_m_variation import adaptive_modulation
from enhanced_spectrum_sensing import spectrum_sensor
from intelligent_jammer_detector import jammer_detector


def np_time() -> float:
    """Get current time for consistency"""
    return np.float64(time.time())


class ThreatLevel(Enum):
    """Unified threat level classification"""
    NONE = 0       # No threat detected
    LOW = 1        # Light interference, possible noise
    MEDIUM = 2     # Moderate jamming detected
    HIGH = 3       # Strong jamming signature
    CRITICAL = 4   # Severe coordinated jamming


@dataclass
class HybridThreatAssessment:
    """Unified threat assessment from all detectors"""
    # ML-based threat from RF/XGBoost models
    ml_threat_probability: float  # 0-1 from threat_model_runtime
    ml_threat_level: str          # VERY_LOW/LOW/MEDIUM/HIGH/VERY_HIGH
    
    # Spectrum sensing results
    spectrum_state: str           # IDLE/BUSY/JAMMED
    spectrum_confidence: float    # 0-1
    interference_type: str        # NONE/AWGN/NARROWBAND/WIDEBAND/IMPULSE
    
    # Intelligent detector results
    feature_threat_confidence: float  # 0-1
    feature_threat_level: str        # VERY_LOW/LOW/MEDIUM/HIGH/VERY_HIGH
    
    # Unified assessment
    unified_threat_level: ThreatLevel
    unified_confidence: float
    recommended_m: int            # 16, 64, or 256
    recommended_action: str       # TRANSMIT/WAIT/ROBUST/SHUTDOWN
    
    # Component voting results
    all_agree_jammed: bool
    agreement_score: float        # 0-1, how much do detectors agree
    
    # Timing
    assessment_timestamp: float
    


class HybridAntiJammingManager:
    """
    Unified anti-jamming manager combining multiple detection techniques.
    
    Strategy:
    1. Use threat_model_runtime for ML threat probability (trained RF/XGBoost)
    2. Use spectrum_sensor for channel state classification
    3. Use jammer_detector for feature-based threat scoring
    4. Combine via voting/ensemble for robust decision
    5. Recommend M based on unified threat level
    6. Track metrics for visualization
    """
    
    def __init__(self, model_dir: str = None, enable_ml_runtime: bool = True):
        self.model_dir = model_dir or "."
        self.enable_ml_runtime = enable_ml_runtime
        
        # Initialize ML runtime (with trained RF/XGBoost)
        self.ml_runtime = ThreatModelRuntime(
            model_dir=self.model_dir,
            threshold=0.65
        ) if enable_ml_runtime else None
        
        # History tracking
        self.assessment_history = deque(maxlen=100)
        self.threat_level_history = deque(maxlen=100)
        self.m_selection_history = deque(maxlen=100)
        self.lock = threading.Lock()
        
        # Statistics
        self.total_assessments = 0
        self.high_threat_count = 0
        self.low_threat_count = 0
        self.agreement_count = 0
        self.disagreement_count = 0
    
    def assess_packet(
        self,
        ofdm_signal: np.ndarray,
        sensing_energy: Optional[float] = None,
        signal_strength_db: Optional[float] = None,
    ) -> HybridThreatAssessment:
        """
        Comprehensive threat assessment from all detectors.
        
        Args:
            ofdm_signal: Complex OFDM signal samples
            sensing_energy: Optional sensed channel energy
            signal_strength_db: Optional measured SINR in dB
            
        Returns:
            HybridThreatAssessment with unified recommendation
        """
        
        assessment_timestamp = np_time()
        
        # ============ ML THREAT DETECTION (threat_model_runtime) ============
        ml_threat_prob = 0.0
        ml_threat_level = "VERY_LOW"
        if self.ml_runtime and self.ml_runtime.enabled:
            ml_prediction = self.ml_runtime.score_packet(
                ofdm_signal=ofdm_signal,
                sensing_energy=sensing_energy,
                scan_type=1.0
            )
            if ml_prediction:
                ml_threat_prob = ml_prediction.ensemble_probability
                # Map to threat level names
                if ml_threat_prob < 0.2:
                    ml_threat_level = "VERY_LOW"
                elif ml_threat_prob < 0.4:
                    ml_threat_level = "LOW"
                elif ml_threat_prob < 0.6:
                    ml_threat_level = "MEDIUM"
                elif ml_threat_prob < 0.8:
                    ml_threat_level = "HIGH"
                else:
                    ml_threat_level = "VERY_HIGH"
        
        # ============ SPECTRUM SENSING ============
        spectrum_state = "IDLE"
        spectrum_confidence = 0.0
        interference_type = "NONE"
        if len(ofdm_signal) > 0:
            spectrum_result = spectrum_sensor.sense_channel(ofdm_signal)
            raw_state = spectrum_result.get('state', 'IDLE')
            spectrum_state = raw_state.name if hasattr(raw_state, 'name') else str(raw_state)
            spectrum_confidence = 1.0 if spectrum_result.get('is_jammed', False) else 0.2
            raw_interference = spectrum_result.get('interference_type', 'NONE')
            interference_type = raw_interference.name if hasattr(raw_interference, 'name') else str(raw_interference)
        
        # ============ FEATURE-BASED ML DETECTION ============
        feature_threat_conf = 0.0
        feature_threat_level = "VERY_LOW"
        if len(ofdm_signal) > 0:
            ml_result = jammer_detector.detect_jamming(ofdm_signal)
            feature_threat_conf = ml_result.get('confidence', 0.0)
            confidence_enum = ml_result.get('confidence_enum', None)
            if confidence_enum:
                feature_threat_level = confidence_enum.name
        
        # ============ UNIFIED THREAT ASSESSMENT ============
        # Determine agreement between detectors
        is_jammed_ml = ml_threat_prob >= 0.65
        is_jammed_spectrum = spectrum_state in ["BUSY", "JAMMED"]
        is_jammed_feature = feature_threat_conf >= 0.6
        
        all_agree_jammed = is_jammed_ml and is_jammed_spectrum and is_jammed_feature
        agree_count = sum([is_jammed_ml, is_jammed_spectrum, is_jammed_feature])
        agreement_score = agree_count / 3.0
        
        # Calculate unified threat level (0-1)
        unified_confidence = (ml_threat_prob + spectrum_confidence + feature_threat_conf) / 3.0
        
        # Map to threat level
        if all_agree_jammed:
            unified_threat_level = ThreatLevel.CRITICAL
        elif agreement_score >= 0.66:  # 2 or 3 agree
            unified_threat_level = ThreatLevel.HIGH
        elif unified_confidence >= 0.65:
            unified_threat_level = ThreatLevel.MEDIUM
        elif unified_confidence >= 0.35:
            unified_threat_level = ThreatLevel.LOW
        else:
            unified_threat_level = ThreatLevel.NONE
        
        # ============ MODULATION RECOMMENDATION ============
        recommended_m = self._recommend_m(
            unified_threat_level,
            signal_strength_db
        )
        
        # ============ ACTION RECOMMENDATION ============
        recommended_action = self._recommend_action(
            unified_threat_level,
            spectrum_state,
            all_agree_jammed
        )
        
        # ============ CREATE ASSESSMENT ============
        assessment = HybridThreatAssessment(
            ml_threat_probability=ml_threat_prob,
            ml_threat_level=ml_threat_level,
            spectrum_state=spectrum_state,
            spectrum_confidence=spectrum_confidence,
            interference_type=interference_type,
            feature_threat_confidence=feature_threat_conf,
            feature_threat_level=feature_threat_level,
            unified_threat_level=unified_threat_level,
            unified_confidence=unified_confidence,
            recommended_m=recommended_m,
            recommended_action=recommended_action,
            all_agree_jammed=all_agree_jammed,
            agreement_score=agreement_score,
            assessment_timestamp=assessment_timestamp
        )
        
        # ============ RECORD METRICS ============
        with self.lock:
            self.assessment_history.append(assessment)
            self.threat_level_history.append(unified_threat_level)
            self.m_selection_history.append(recommended_m)
            self.total_assessments += 1
            
            if unified_threat_level == ThreatLevel.CRITICAL:
                self.high_threat_count += 1
            elif unified_threat_level == ThreatLevel.NONE:
                self.low_threat_count += 1
            
            if agreement_score >= 0.66:
                self.agreement_count += 1
            else:
                self.disagreement_count += 1
        
        return assessment
    
    def _recommend_m(self, threat_level: ThreatLevel, signal_strength_db: Optional[float] = None) -> int:
        """Recommend modulation based on threat level"""
        
        # Use signal strength if available
        if signal_strength_db is not None:
            if signal_strength_db < -5.0:
                return 16
            elif signal_strength_db < 5.0:
                return 64
            else:
                return 256
        
        # Use threat level
        if threat_level == ThreatLevel.NONE:
            return 256  # Efficient
        elif threat_level == ThreatLevel.LOW:
            return 256  # Slight margin
        elif threat_level == ThreatLevel.MEDIUM:
            return 64   # Balanced
        elif threat_level == ThreatLevel.HIGH:
            return 16   # Robust
        else:  # CRITICAL
            return 16   # Most robust
    
    def _recommend_action(self, threat_level: ThreatLevel, spectrum_state: str, all_agree: bool) -> str:
        """Recommend transmission action"""
        
        if threat_level == ThreatLevel.NONE:
            return "TRANSMIT"
        elif threat_level == ThreatLevel.LOW:
            return "TRANSMIT" if spectrum_state == "IDLE" else "WAIT"
        elif threat_level == ThreatLevel.MEDIUM:
            return "ROBUST"   # Transmit with reduced M
        elif threat_level == ThreatLevel.HIGH:
            return "ROBUST" if not all_agree else "WAIT"
        else:  # CRITICAL
            return "WAIT"
    
    def get_diagnostics(self) -> Dict:
        """Export diagnostics for visualization"""
        with self.lock:
            if not self.assessment_history:
                return {
                    'total_assessments': 0,
                    'threat_levels': [],
                    'm_selections': [],
                    'agreement_score': 0.0
                }
            
            threat_levels = list(self.threat_level_history)
            threat_names = [tl.name for tl in threat_levels]
            m_selections = list(self.m_selection_history)
            
            recent_agreements = (
                self.agreement_count / max(self.total_assessments, 1)
            ) if self.total_assessments > 0 else 0.0
            
            return {
                'total_assessments': self.total_assessments,
                'threat_levels': threat_names[-20:],  # Last 20
                'm_selections': m_selections[-20:],
                'high_threat_events': self.high_threat_count,
                'low_threat_events': self.low_threat_count,
                'agreement_score': recent_agreements,
                'ml_runtime_enabled': self.ml_runtime and self.ml_runtime.enabled,
            }
    
    def reset_history(self):
        """Clear history for new session"""
        with self.lock:
            self.assessment_history.clear()
            self.threat_level_history.clear()
            self.m_selection_history.clear()
            self.total_assessments = 0
            self.high_threat_count = 0
            self.low_threat_count = 0
            self.agreement_count = 0
            self.disagreement_count = 0


# Global instance
hybrid_manager = HybridAntiJammingManager(enable_ml_runtime=True)
