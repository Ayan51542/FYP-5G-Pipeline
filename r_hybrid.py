# r_hybrid.py - ML-Hybrid Receiver (Socket-Based)
# Full networking from r.py + hybrid_anti_jamming_manager for threat correlation

print("\n[DEBUG] HYBRID RECEIVER: LOADING...\n")

import socket, struct, threading, json, hashlib, random, re, os, time, datetime
from reedsolo import RSCodec
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from Crypto.Protocol.KDF import PBKDF2
import numpy as np

# --- ANTI-JAMMING: Hybrid Manager ---
from hybrid_anti_jamming_manager import hybrid_manager, ThreatLevel
from adaptive_m_variation import adaptive_modulation
from enhanced_spectrum_sensing import spectrum_sensor
from intelligent_jammer_detector import jammer_detector

# --- PLOTTING ---
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import glob

# ---------- Config ----------
BS1_HOST, BS1_PORT = "127.0.0.1", 50050
BS2_HOST, BS2_PORT = "127.0.0.1", 50051
PRIMARY_SENDERS = ["JAZZ", "UFONE", "TELENOR", "WARID", "STARLINK", "ZONG", "SCO", "PTCL"]

# ---------- Crypto ----------
PASSPHRASE = b"very secret passphrase - change this!"
SALT = b'\x00'*16
KEY = PBKDF2(PASSPHRASE, SALT, dkLen=32, count=100000)
rs = RSCodec(40)
rs_robust = RSCodec(80)  # For decoding RS-80 packets from BS during jamming
stop_event = threading.Event()
plot_lock = threading.Lock()
log_lock = threading.Lock()

# --- Session State ---
NODE_ID = ""
chat_partner = None
pending_request_from = None
state_lock = threading.Lock()

CTL_CONNECT_REQUEST = "__CONNECT_REQUEST__"
CTL_CONNECT_ACCEPT = "__CONNECT_ACCEPT__"
CTL_CONNECT_REJECT = "__CONNECT_REJECT__"
CTL_DISCONNECT = "__DISCONNECT__"
CTL_RETRANSMIT = "__RETRANSMIT_REQUEST__"

# --- Hybrid threat correlation ---
HYBRID_THREAT_EVENTS = []
CONSECUTIVE_FAILURES = 0
ANTI_JAM_STATS = {'nacks_sent': 0, 'rs80_recoveries': 0, 'sustained_jamming_alerts': 0}

# ---------- Helpers ----------
def aes_gcm_encrypt(plaintext, key):
    nonce = get_random_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return nonce + tag + ciphertext

def aes_gcm_decrypt(enc_blob, key):
    nonce, tag, ciphertext = enc_blob[:12], enc_blob[12:28], enc_blob[28:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag)

def determine_M(msg): return 256 if len(msg) > 500 else (64 if len(msg) > 100 else 16)

def recv_full(sock, length):
    data = b""
    while len(data) < length:
        try:
            more = sock.recv(length - len(data))
            if not more: raise ConnectionError("socket closed")
            data += more
        except socket.timeout: return None
    return data

# ---------- PHY ----------
def bits_from_bytes(b): return np.unpackbits(np.frombuffer(b, dtype=np.uint8))
def qam_mod(bits, M):
    k = int(np.log2(M))
    if len(bits)%k!=0: bits = np.concatenate([bits, np.zeros(k-(len(bits)%k), dtype=np.uint8)])
    ints = bits.reshape((-1,k)).dot(1<<np.arange(k-1,-1,-1))
    sqrtM = int(np.sqrt(M))
    scale = np.sqrt((2.0/3.0)*(M-1)) if M>1 else 1.0
    return ((2*(ints%sqrtM)-(sqrtM-1))/scale) + 1j*((2*(ints//sqrtM)-(sqrtM-1))/scale)
def ofdm_mod(symbols, nc, cp):
    if len(symbols)==0: return np.array([])
    n = int(np.ceil(len(symbols)/nc))
    padded = np.pad(symbols, (0, n*nc-len(symbols)))
    ifft = np.fft.ifft(padded.reshape((n, nc)), axis=1)
    return np.hstack([ifft[:, -cp:], ifft]).flatten()
def compute_ofdm_energy(msg_bytes, M, nc, cp):
    bits = bits_from_bytes(msg_bytes)
    tx_syms = qam_mod(bits, M)
    ofdm_sig = ofdm_mod(tx_syms, nc, cp)
    return (np.mean(np.abs(ofdm_sig)**2) if ofdm_sig.size>0 else 0.0), ofdm_sig

# ---------- Plotting ----------
DATA_DIR = "node_logs"
os.makedirs(DATA_DIR, exist_ok=True)
messages_by_peer = {}
base_station_events = []

def cleanup_old_files(node_id):
    for f in glob.glob(os.path.join(DATA_DIR, f"{node_id}_*")):
        try: os.remove(f)
        except: pass

def log_event(txt): base_station_events.append(f"{datetime.datetime.utcnow().isoformat()}Z {txt}")

def record_message(peer_id, text_bytes, M, message_text, is_jammed=False):
    entry = {"ts": time.time(), "bytes": text_bytes, "M": M, "text": message_text}
    try:
        bits = bits_from_bytes(text_bytes)
        tx_symbols = qam_mod(bits, M)
        nc, cp = 64, 8
        if is_jammed:
            noise = (np.random.normal(0, 0.5, tx_symbols.shape) +
                     1j * np.random.normal(0, 0.5, tx_symbols.shape))
            tx_symbols = tx_symbols + noise
        ofdm_sig = ofdm_mod(tx_symbols, nc, cp)
        entry["nc"], entry["cp"], entry["tx_symbols"], entry["ofdm_sig"] = nc, cp, tx_symbols, ofdm_sig
    except:
        entry["tx_symbols"], entry["ofdm_sig"] = np.array([]), np.array([])
        entry["nc"], entry["cp"] = 0, 0
    with log_lock:
        messages_by_peer.setdefault(peer_id, []).append(entry)

def make_constellation_plot(symbols, title, message_text, M, nc, cp):
    fig = plt.figure(figsize=(8,6))
    if len(symbols) > 0:
        color = 'red' if "[JAMMED]" in message_text else 'blue'
        alpha = 0.3 if "[JAMMED]" in message_text else 1.0
        plt.scatter(np.real(symbols), np.imag(symbols), s=10, c=color, alpha=alpha)
    plt.title(title); plt.grid(True)
    textstr = f'Msg: "{message_text[:30]}..."\nMod: QAM-{M}\nNC: {nc}, CP: {cp}'
    plt.text(0.05, 0.95, textstr, transform=plt.gca().transAxes, fontsize=9, verticalalignment='top',
             bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))
    return fig

def make_ofdm_plot(signal, title, message_text, M, nc, cp):
    fig = plt.figure(figsize=(8,4))
    if len(signal) > 0:
        limit = min(300, len(signal))
        plt.plot(np.real(signal[:limit]), label="I"); plt.plot(np.imag(signal[:limit]), label="Q")
    plt.title(title); plt.grid(True); plt.legend()
    return fig

def make_psd_plot(ofdm_sig, title):
    fig = plt.figure(figsize=(8,4))
    if ofdm_sig.size > 0:
        fft_result = np.abs(np.fft.fft(ofdm_sig))**2
        freq = np.fft.fftfreq(len(fft_result))
        plt.semilogy(freq[:len(freq)//2], fft_result[:len(fft_result)//2], color='darkblue')
    plt.title(title); plt.xlabel("Normalized Frequency"); plt.ylabel("PSD")
    plt.grid(True, alpha=0.3)
    return fig

def make_jammed_vs_clean_comparison(peer_data):
    fig = plt.figure(figsize=(10,6))
    jammed = sum(1 for p, entries in peer_data.items() for e in entries if "[JAMMED]" in e.get("text",""))
    clean = sum(1 for p, entries in peer_data.items() for e in entries if "[JAMMED]" not in e.get("text",""))
    plt.bar(["Clean", "Jammed"], [clean, jammed], color=['green','red'], alpha=0.7, edgecolor='black')
    plt.title("Packet Reception: Clean vs Jammed"); plt.ylabel("Count")
    for i, v in enumerate([clean, jammed]):
        plt.text(i, v+0.5, str(v), ha='center', fontweight='bold')
    plt.grid(True, alpha=0.3, axis='y')
    return fig

def make_hybrid_threat_correlation_plot():
    """Plot threat level vs decryption success correlation"""
    if not HYBRID_THREAT_EVENTS:
        return None
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle('Hybrid Threat vs Decryption Correlation', fontsize=14, fontweight='bold')

    ml_probs = [t['ml_prob'] for t in HYBRID_THREAT_EVENTS]
    success = [1.0 if t['decode_success'] else 0.0 for t in HYBRID_THREAT_EVENTS]
    confidences = [t['confidence'] for t in HYBRID_THREAT_EVENTS]
    agreements = [t['agreement'] for t in HYBRID_THREAT_EVENTS]

    # Threat vs Success
    colors = ['green' if s else 'red' for s in success]
    axes[0,0].scatter(ml_probs, success, c=colors, alpha=0.6, s=60)
    axes[0,0].set_xlabel('ML Threat Probability'); axes[0,0].set_ylabel('Decode Success')
    axes[0,0].set_title('Threat vs Decode'); axes[0,0].grid(True, alpha=0.3)

    # Confidence histogram
    axes[0,1].hist(confidences, bins=20, color='blue', alpha=0.7, edgecolor='black')
    axes[0,1].set_xlabel('Confidence'); axes[0,1].set_ylabel('Count')
    axes[0,1].set_title('Confidence Distribution'); axes[0,1].grid(True, alpha=0.3)

    # Agreement histogram
    axes[1,0].hist(agreements, bins=20, color='green', alpha=0.7, edgecolor='black')
    axes[1,0].set_xlabel('Agreement Score'); axes[1,0].set_ylabel('Count')
    axes[1,0].set_title('Detector Agreement'); axes[1,0].grid(True, alpha=0.3)

    # Threat level at failures
    fail_levels = {'NONE':0, 'LOW':0, 'MEDIUM':0, 'HIGH':0, 'CRITICAL':0}
    for t in HYBRID_THREAT_EVENTS:
        if not t['decode_success']:
            fail_levels[t['threat_level']] = fail_levels.get(t['threat_level'], 0) + 1
    bars = axes[1,1].bar(fail_levels.keys(), fail_levels.values(),
                         color=['green','yellow','orange','red','darkred'], alpha=0.7, edgecolor='black')
    axes[1,1].set_xlabel('Threat Level'); axes[1,1].set_ylabel('Failures')
    axes[1,1].set_title('Failures by Threat Level'); axes[1,1].grid(True, alpha=0.3)

    plt.tight_layout()
    return fig

def make_anti_jamming_summary():
    """Anti-jamming technique summary plots"""
    figs = []
    # Adaptive Modulation
    fig = plt.figure(figsize=(10, 6))
    am_diag = adaptive_modulation.get_diagnostics()
    stats_text = f"""
    ADAPTIVE M VARIATION STATISTICS
    Current M: QAM-{am_diag['current_m']}
    Success Rate: {am_diag['success_rate']:.1%}
    Jammed Frames: {am_diag['jammed_frames']}
    Clean Frames: {am_diag['clean_frames']}
    Last SINR: {am_diag['last_sinr_db']:.2f} dB
    Total M Switches: {am_diag['total_m_switches']}
    """
    plt.text(0.05, 0.95, stats_text, transform=fig.transFigure, fontfamily='monospace', fontsize=10,
             verticalalignment='top', bbox=dict(boxstyle='round', facecolor='lightblue', alpha=0.8))
    plt.axis('off'); plt.title("Adaptive Modulation Summary", fontsize=14, fontweight='bold')
    figs.append(fig)

    # Spectrum Sensing
    fig = plt.figure(figsize=(10, 6))
    ss_diag = spectrum_sensor.get_diagnostics()
    sensing_text = f"""
    ENHANCED SPECTRUM SENSING
    Channel State: {ss_diag['last_channel_state'].name}
    Measurements: {ss_diag['measurements']}
    Avg Power: {ss_diag['average_power']:.2e} W
    Jam Detections: {ss_diag['jam_detections']}
    Detection Rate: {ss_diag['jam_detection_rate']:.1%}
    """
    plt.text(0.05, 0.95, sensing_text, transform=fig.transFigure, fontfamily='monospace', fontsize=10,
             verticalalignment='top', bbox=dict(boxstyle='round', facecolor='lightgreen', alpha=0.8))
    plt.axis('off'); plt.title("Spectrum Sensing Summary", fontsize=14, fontweight='bold')
    figs.append(fig)

    # Hybrid Manager
    fig = plt.figure(figsize=(10, 6))
    hm_diag = hybrid_manager.get_diagnostics()
    hybrid_text = f"""
    HYBRID ANTI-JAMMING MANAGER
    Total Assessments: {hm_diag['total_assessments']}
    High Threat Events: {hm_diag['high_threat_events']}
    Low Threat Events: {hm_diag['low_threat_events']}
    Agreement Score: {hm_diag['agreement_score']:.1%}
    ML Runtime: {'Enabled' if hm_diag['ml_runtime_enabled'] else 'Disabled'}
    """
    plt.text(0.05, 0.95, hybrid_text, transform=fig.transFigure, fontfamily='monospace', fontsize=10,
             verticalalignment='top', bbox=dict(boxstyle='round', facecolor='lightyellow', alpha=0.8))
    plt.axis('off'); plt.title("Hybrid Manager Summary", fontsize=14, fontweight='bold')
    figs.append(fig)

    return figs

def export_all_results(node_id):
    print(f"\n[EXPORT] Generating comprehensive hybrid receiver plots...")
    figs = []

    # Anti-jamming summaries
    try: figs.extend(make_anti_jamming_summary())
    except Exception as e: print(f"[WARN] Anti-jamming summary failed: {e}")

    # Hybrid threat correlation
    try:
        hybrid_fig = make_hybrid_threat_correlation_plot()
        if hybrid_fig: figs.append(hybrid_fig)
    except Exception as e: print(f"[WARN] Threat correlation plot failed: {e}")

    with log_lock: peer_data = dict(messages_by_peer)

    # Individual message plots
    for peer, entries in peer_data.items():
        for i, e in enumerate(entries):
            try:
                if e.get("tx_symbols", np.array([])).size > 0:
                    status = "JAMMED" if "[JAMMED]" in e["text"] else "CLEAN"
                    figs.append(make_constellation_plot(e["tx_symbols"], f"[{peer}] [{status}] Constellation ({i})", e["text"], e["M"], e["nc"], e["cp"]))
                    figs.append(make_ofdm_plot(e["ofdm_sig"], f"[{peer}] [{status}] OFDM ({i})", e["text"], e["M"], e["nc"], e["cp"]))
                    figs.append(make_psd_plot(e["ofdm_sig"], f"[{peer}] [{status}] PSD ({i})"))
            except: pass

    # Aggregate
    try:
        if peer_data: figs.append(make_jammed_vs_clean_comparison(peer_data))
    except: pass

    timestamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    pdf_path = os.path.join(DATA_DIR, f"{node_id}_hybrid_plots_{timestamp}.pdf")
    with plot_lock:
        try:
            with PdfPages(pdf_path) as pdf:
                for f in figs: pdf.savefig(f); plt.close(f)
        except Exception as e: print(f"[ERROR] Plot saving failed: {e}")

    # Save logs
    with open(os.path.join(DATA_DIR, f"{node_id}_hybrid_log_{timestamp}.txt"), "w") as f:
        f.write(f"=== HYBRID RECEIVER {node_id} ===\n\n")
        for ev in base_station_events: f.write(ev + "\n")
        f.write("\n=== RECEIVED MESSAGES ===\n")
        for peer, entries in peer_data.items():
            f.write(f"\nFrom {peer} ({len(entries)} messages):\n")
            for e in entries:
                status = "JAMMED" if "[JAMMED]" in e["text"] else "CLEAN"
                f.write(f"  [{status}] {e['text']} (QAM-{e['M']})\n")

    # Save hybrid JSON
    json_path = os.path.join(DATA_DIR, f"{node_id}_hybrid_results_{timestamp}.json")
    try:
        results = {
            'implementation': 'ML-Hybrid Receiver (socket-based)',
            'total_threat_events': len(HYBRID_THREAT_EVENTS),
            'decode_successes': sum(1 for t in HYBRID_THREAT_EVENTS if t['decode_success']),
            'decode_failures': sum(1 for t in HYBRID_THREAT_EVENTS if not t['decode_success']),
        }
        with open(json_path, 'w') as f: json.dump(results, f, indent=2)
    except: pass

    print(f"[EXPORT] Saved {len(figs)} plots to {pdf_path}")

# ---------- Networking ----------
def send_message(sock, recipient, message_text):
    try:
        M = determine_M(message_text); nc, cp = 64, 8
        msg_bytes = message_text.encode("utf-8")
        energy, _ = compute_ofdm_energy(msg_bytes, M, nc, cp)
        pkt = struct.pack(">H", len(msg_bytes)) + msg_bytes
        plaintext = struct.pack(">H H B", M, nc, cp) + pkt
        enc = aes_gcm_encrypt(plaintext, KEY)
        enc_rs = rs.encode(enc)
        dst_b = recipient.encode("utf-8")
        payload = struct.pack(">H", len(dst_b)) + dst_b + struct.pack(">d", float(energy)) + enc_rs
        sock.sendall(struct.pack(">I", len(payload)) + payload)
        if message_text not in [CTL_CONNECT_REQUEST, CTL_CONNECT_ACCEPT, CTL_CONNECT_REJECT, CTL_DISCONNECT, CTL_RETRANSMIT]:
            record_message(recipient, msg_bytes, M, message_text)
        return True
    except: return False

def receive_handler(sock):
    global chat_partner, pending_request_from
    sock.settimeout(2)
    while not stop_event.is_set():
        try:
            hdr = recv_full(sock, 4)
            if not hdr: continue
            length = struct.unpack(">I", hdr)[0]
            data = recv_full(sock, length)

            src_len = struct.unpack(">H", data[:2])[0]
            src_id = data[2:2+src_len].decode("utf-8")
            pos = 2 + src_len + 8

            try:
                plaintext = aes_gcm_decrypt(rs.decode(data[pos:])[0], KEY)
                pkt = plaintext[5:]; msg_len = struct.unpack(">H", pkt[:2])[0]
                msg = pkt[2:2+msg_len].decode("utf-8")
                decode_success = True
            except:
                # --- ANTI-JAM: Try RS-80 fallback decode (BS may have upgraded RS) ---
                try:
                    plaintext = aes_gcm_decrypt(rs_robust.decode(data[pos:])[0], KEY)
                    pkt = plaintext[5:]; msg_len = struct.unpack(">H", pkt[:2])[0]
                    msg = pkt[2:2+msg_len].decode("utf-8")
                    decode_success = True
                    ANTI_JAM_STATS['rs80_recoveries'] += 1
                    print(f"\n[ANTI-JAM] RS-80 recovery successful from {src_id}!", end="")
                except:
                    decode_success = False
                    msg = None

            if not decode_success:
                CONSECUTIVE_FAILURES_local = globals().get('CONSECUTIVE_FAILURES', 0) + 1
                globals()['CONSECUTIVE_FAILURES'] = CONSECUTIVE_FAILURES_local

                # --- HYBRID JAMMING DIAGNOSIS ---
                try:
                    test_signal = np.random.normal(0, 1e-6, 256) + 1j*np.random.normal(0, 1e-6, 256)
                    assessment = hybrid_manager.assess_packet(
                        ofdm_signal=test_signal,
                        sensing_energy=np.mean(np.abs(test_signal)**2),
                    )
                    HYBRID_THREAT_EVENTS.append({
                        'src': src_id, 'ts': time.time(),
                        'ml_prob': float(assessment.ml_threat_probability),
                        'threat_level': assessment.unified_threat_level.name,
                        'confidence': float(assessment.unified_confidence),
                        'agreement': float(assessment.agreement_score),
                        'decode_success': False,
                    })
                    print(f"\n[HYBRID] CORRUPTED from {src_id}: "
                          f"Threat={assessment.unified_threat_level.name} "
                          f"ML={assessment.ml_threat_probability:.3f} "
                          f"Agreement={assessment.agreement_score:.2f}")
                except: pass

                # --- ANTI-JAM Strategy 5: Send NACK retransmission request ---
                print(f"[!] PACKET CORRUPTED/JAMMED FROM {src_id} [!]")
                ANTI_JAM_STATS['nacks_sent'] += 1
                send_message(sock, src_id, CTL_RETRANSMIT)
                print(f"[ANTI-JAM] Sent RETRANSMIT request to {src_id} (failure #{CONSECUTIVE_FAILURES_local})")

                if CONSECUTIVE_FAILURES_local >= 3:
                    ANTI_JAM_STATS['sustained_jamming_alerts'] += 1
                    print(f"[ANTI-JAM] !!! SUSTAINED JAMMING DETECTED !!! ({CONSECUTIVE_FAILURES_local} consecutive failures)")

                print("> ", end="", flush=True)
                dummy_bytes = b'\x00' * 64
                record_message(src_id, dummy_bytes, 16, f"[JAMMED] CORRUPTED DATA", is_jammed=True)
                log_event(f"JAMMED packet from {src_id} (NACK sent)")
                adaptive_modulation.log_frame_result(success=False, jammed=True)
                continue

            # Successful decode — reset consecutive failure counter
            globals()['CONSECUTIVE_FAILURES'] = 0

            # Successful decode - run hybrid assessment for correlation tracking
            try:
                msg_bytes = msg.encode("utf-8")
                _, ofdm_sig = compute_ofdm_energy(msg_bytes, determine_M(msg), 64, 8)
                if ofdm_sig.size > 0:
                    assessment = hybrid_manager.assess_packet(
                        ofdm_signal=ofdm_sig,
                        sensing_energy=np.mean(np.abs(ofdm_sig)**2),
                    )
                    HYBRID_THREAT_EVENTS.append({
                        'src': src_id, 'ts': time.time(),
                        'ml_prob': float(assessment.ml_threat_probability),
                        'threat_level': assessment.unified_threat_level.name,
                        'confidence': float(assessment.unified_confidence),
                        'agreement': float(assessment.agreement_score),
                        'decode_success': True,
                    })
            except: pass

            if msg == CTL_CONNECT_REQUEST:
                is_primary = src_id in PRIMARY_SENDERS
                with state_lock:
                    if chat_partner and is_primary and (chat_partner not in PRIMARY_SENDERS):
                        print(f"\n\n[PRIORITY] Primary '{src_id}' preempting Secondary '{chat_partner}'!")
                        send_message(sock, chat_partner, CTL_DISCONNECT)
                        chat_partner = None
                    if not chat_partner and not pending_request_from:
                        pending_request_from = src_id
                        print(f"\n[!] Request from {src_id}. 'accept'/'reject'?\n> ", end="", flush=True)
                continue

            if msg == CTL_DISCONNECT:
                with state_lock:
                    if src_id == chat_partner: print(f"\n[INFO] {chat_partner} disconnected.\n> ", end="", flush=True); chat_partner = None
                continue

            if msg == CTL_CONNECT_ACCEPT: pass

            with state_lock:
                if src_id == chat_partner:
                    print(f"\n<<< {src_id}: '{msg}'\n> ", end="", flush=True)
                    record_message(src_id, msg.encode("utf-8"), determine_M(msg), msg)
                    log_event(f"Received from {src_id}: {msg}")

        except socket.timeout: continue
        except: continue

def main_loop(sock):
    global chat_partner, pending_request_from
    while not stop_event.is_set():
        try:
            with state_lock: curr_p = chat_partner; curr_req = pending_request_from
            prompt = "> " if curr_p else (f"Accept {curr_req}? (accept/reject): " if curr_req else "Waiting... ('exit'): ")
            user_input = input(prompt).strip()
            if not user_input: continue
            if user_input.lower() == 'exit':
                if curr_p: send_message(sock, curr_p, CTL_DISCONNECT)
                stop_event.set(); break

            if curr_req:
                if user_input.lower() == 'accept':
                    send_message(sock, curr_req, CTL_CONNECT_ACCEPT)
                    with state_lock: chat_partner = curr_req; pending_request_from = None
                    print(f"[SUCCESS] Connected with {chat_partner}.")
                else:
                    send_message(sock, curr_req, CTL_CONNECT_REJECT)
                    with state_lock: pending_request_from = None
                    print("[INFO] Rejected.")
            elif curr_p: send_message(sock, curr_p, user_input)

        except (EOFError, KeyboardInterrupt): stop_event.set()
        except Exception as e: print(f"[ERROR] {e}"); stop_event.set()

def main():
    global NODE_ID
    node_id = ""
    while not re.match(r"^R([1-9]|[1-5][0-9]|60)$", node_id):
        node_id = input("Enter Receiver ID (R1-R60): ").strip().upper()
    NODE_ID = node_id

    cleanup_old_files(node_id)
    BS_HOST, BS_PORT = BS1_HOST, BS1_PORT

    try:
        sock = socket.create_connection((BS_HOST, BS_PORT), timeout=5)
        sock.sendall((json.dumps({"type": "receiver", "id": node_id, "pos": (random.uniform(0,100), 0)}) + "\n").encode("utf-8"))
        print(f"\n{'='*70}")
        print(f"ML-HYBRID RECEIVER {node_id} (socket-based)")
        print(f"Using hybrid_anti_jamming_manager for threat correlation")
        print(f"Registered with BS at {BS_HOST}:{BS_PORT}")
        print(f"{'='*70}\n")
    except Exception as e:
        print(f"[ERROR] Could not connect to BS: {e}")
        return

    threading.Thread(target=receive_handler, args=(sock,), daemon=True).start()
    try: main_loop(sock)
    finally: stop_event.set(); export_all_results(node_id); sock.close()

if __name__ == "__main__":
    main()
