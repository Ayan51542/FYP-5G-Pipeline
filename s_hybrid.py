# s_hybrid.py - ML-Hybrid Sender (Socket-Based)
# Full networking from s.py + hybrid_anti_jamming_manager for channel assessment

print("\n[DEBUG] HYBRID SENDER: LOADING...\n")

import socket, struct, threading, json, hashlib, random, re, os, time, datetime
from reedsolo import RSCodec
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from Crypto.Protocol.KDF import PBKDF2
import numpy as np

# --- ANTI-JAMMING: Hybrid Manager (replaces individual modules) ---
from hybrid_anti_jamming_manager import hybrid_manager, ThreatLevel
from adaptive_m_variation import adaptive_modulation

# --- PLOTTING ---
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import glob

# --- Config ---
BS1_HOST, BS1_PORT = "127.0.0.1", 50050
BS2_HOST, BS2_PORT = "127.0.0.1", 50051
PRIMARY_SENDERS = ["JAZZ", "UFONE", "TELENOR", "WARID", "STARLINK", "ZONG", "SCO", "PTCL"]

# ---------- Crypto ----------
PASSPHRASE = b"very secret passphrase - change this!"
SALT = b'\x00'*16
KEY = PBKDF2(PASSPHRASE, SALT, dkLen=32, count=100000)
rs = RSCodec(40)
stop_event = threading.Event()
plot_lock = threading.Lock()
log_lock = threading.Lock()

# --- Session ---
NODE_ID = ""; NODE_POS = (100.0, 0.0)
chat_partner = None
connection_status = threading.Event()
connection_accepted = False
state_lock = threading.Lock()

CTL_CONNECT_REQUEST = "__CONNECT_REQUEST__"
CTL_CONNECT_ACCEPT = "__CONNECT_ACCEPT__"
CTL_CONNECT_REJECT = "__CONNECT_REJECT__"
CTL_DISCONNECT = "__DISCONNECT__"
CTL_RETRANSMIT = "__RETRANSMIT_REQUEST__"

# --- Hybrid assessment history ---
HYBRID_ASSESSMENTS = []
LAST_SENT_MESSAGE = {}  # {recipient: message_text} for retransmission
ANTI_JAM_STATS = {'deferred': 0, 'robust_downgrades': 0, 'retransmissions': 0}

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
    scale = np.sqrt((2.0/3.0)*(M-1)) if M>1 else 1.0
    return ((2*(ints%int(np.sqrt(M)))-(int(np.sqrt(M))-1))/scale) + 1j*((2*(ints//int(np.sqrt(M)))-(int(np.sqrt(M))-1))/scale)
def ofdm_mod(syms, nc, cp):
    if len(syms)==0: return np.array([])
    n = int(np.ceil(len(syms)/nc))
    padded = np.pad(syms, (0, n*nc-len(syms)))
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
messages_sent = []
base_station_events = []

def log_event(txt): base_station_events.append(f"{datetime.datetime.utcnow().isoformat()}Z {txt}")
def cleanup_old_files(nid):
    for f in glob.glob(os.path.join(DATA_DIR, f"{nid}_*")):
        try: os.remove(f)
        except: pass

def record_message(recip, b, M, txt):
    bits = bits_from_bytes(b); nc=64; cp=8
    tx = qam_mod(bits, M); sig = ofdm_mod(tx, nc, cp)
    with log_lock:
        messages_sent.append({"recipient": recip, "bytes": b, "text": txt, "M": M, "nc": nc, "cp": cp, "tx_syms": tx, "ofdm": sig, "ts": time.time()})

def make_constellation_plot(symbols, title, message_text, M, nc, cp):
    fig = plt.figure(figsize=(8,6))
    if len(symbols) > 0: plt.scatter(np.real(symbols), np.imag(symbols), s=6)
    plt.title(title); plt.grid(True); plt.xlabel("I"); plt.ylabel("Q")
    textstr = f'Msg: "{message_text[:30]}..."\nMod: QAM-{M}\nNC: {nc}, CP: {cp}'
    plt.text(0.05, 0.95, textstr, transform=plt.gca().transAxes, fontsize=9, verticalalignment='top',
             bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))
    return fig

def make_ofdm_plot(signal, title, message_text, M, nc, cp):
    fig = plt.figure(figsize=(8,4))
    if len(signal) > 0:
        limit = min(300, len(signal))
        plt.plot(np.real(signal[:limit]), label="I"); plt.plot(np.imag(signal[:limit]), label="Q")
    plt.title(title); plt.legend(); plt.grid(True)
    return fig

def make_psd_plot(ofdm_sig, title):
    fig = plt.figure(figsize=(8,4))
    if ofdm_sig.size > 0:
        fft_result = np.abs(np.fft.fft(ofdm_sig))**2
        freq = np.fft.fftfreq(len(fft_result))
        plt.semilogy(freq[:len(freq)//2], fft_result[:len(fft_result)//2])
    plt.title(title); plt.xlabel("Normalized Frequency"); plt.ylabel("PSD")
    plt.grid(True, alpha=0.3)
    return fig

def make_hybrid_assessment_plot():
    """Plot hybrid threat assessments over the session"""
    if not HYBRID_ASSESSMENTS:
        return None
    fig, axes = plt.subplots(2, 1, figsize=(12, 8))
    fig.suptitle('Hybrid Threat Assessment (Sender)', fontsize=14, fontweight='bold')

    ml_probs = [a['ml_prob'] for a in HYBRID_ASSESSMENTS]
    levels = [a['threat_level'] for a in HYBRID_ASSESSMENTS]
    actions = [a['action'] for a in HYBRID_ASSESSMENTS]
    ms = [a['recommended_m'] for a in HYBRID_ASSESSMENTS]
    x = list(range(len(HYBRID_ASSESSMENTS)))

    axes[0].plot(x, ml_probs, 'r-o', markersize=4, linewidth=2, label='ML Threat Prob')
    axes[0].axhline(y=0.65, color='orange', linestyle='--', label='Threshold')
    axes[0].set_ylabel('Threat Probability'); axes[0].legend(); axes[0].grid(True, alpha=0.3)

    action_map = {'TRANSMIT': 0, 'ROBUST': 1, 'WAIT': 2, 'SHUTDOWN': 3}
    action_vals = [action_map.get(a, 0) for a in actions]
    action_colors = ['green' if v==0 else 'orange' if v==1 else 'red' if v==2 else 'darkred' for v in action_vals]
    axes[1].scatter(x, ms, c=action_colors, s=80, alpha=0.7, edgecolor='black')
    axes[1].set_ylabel('Recommended M'); axes[1].set_xlabel('Transmission #')
    axes[1].set_yticks([16, 64, 256]); axes[1].grid(True, alpha=0.3)

    plt.tight_layout()
    return fig

def export_all_results(node_id):
    print(f"\n[EXPORT] Generating plots for {len(messages_sent)} messages...")
    figs = []
    with log_lock: msgs_copy = list(messages_sent)

    for i, m in enumerate(msgs_copy):
        try:
            figs.append(make_constellation_plot(m['tx_syms'], f"[MSG {i}] {m['recipient']} Constellation", m['text'], m['M'], m['nc'], m['cp']))
            figs.append(make_ofdm_plot(m['ofdm'], f"[MSG {i}] {m['recipient']} OFDM", m['text'], m['M'], m['nc'], m['cp']))
            figs.append(make_psd_plot(m['ofdm'], f"[MSG {i}] PSD"))
        except Exception as e:
            print(f"[WARN] Failed to plot message {i}: {e}")

    # Add hybrid assessment plot
    hybrid_fig = make_hybrid_assessment_plot()
    if hybrid_fig: figs.append(hybrid_fig)

    timestamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    pdf_path = os.path.join(DATA_DIR, f"{node_id}_hybrid_plots_{timestamp}.pdf")
    with plot_lock:
        try:
            with PdfPages(pdf_path) as pdf:
                for f in figs: pdf.savefig(f); plt.close(f)
        except Exception as e:
            print(f"[ERROR] Plot saving failed: {e}")

    with open(os.path.join(DATA_DIR, f"{node_id}_hybrid_log_{timestamp}.txt"), "w") as f:
        for ev in base_station_events: f.write(ev + "\n")
        for m in msgs_copy: f.write(f"To {m['recipient']}: {m['text']} (QAM-{m['M']}) at {m['ts']}\n")
    print(f"[EXPORT] Saved {len(figs)} plots to {pdf_path}")

# ---------- Networking ----------
def _assess_channel(msg_bytes, nc, cp):
    """Run hybrid assessment and return (assessment, action, M).
    
    IMPORTANT: The sender probes the channel with a noise-floor signal
    (simulating 'listening before talk'), NOT with its own OFDM signal.
    Passing the sender's OFDM signal would cause false JAMMED detections
    because OFDM has a flat spectrum that triggers wideband classification.
    """
    energy_pre, ofdm_pre = compute_ofdm_energy(msg_bytes, 64, nc, cp)
    if ofdm_pre.size == 0:
        return None, "TRANSMIT", adaptive_modulation.get_m_for_transmission(len(msg_bytes))

    # Channel probe: noise-floor signal simulating "listening" to the channel
    # In real SDR, this would be actual received samples from the antenna
    channel_probe = np.random.normal(0, 1e-6, 256) + 1j * np.random.normal(0, 1e-6, 256)

    assessment = hybrid_manager.assess_packet(
        ofdm_signal=channel_probe,
        sensing_energy=energy_pre,
    )

    # Use SINR from spectrum sensing for adaptive M selection
    sinr_db = 10.0 * np.log10(max(energy_pre / 1e-10, 1e-6))  # Estimate from energy
    jammed_recently = assessment.unified_threat_level in [ThreatLevel.HIGH, ThreatLevel.CRITICAL]

    # Strategy 2: Use adapt_m() with real SINR feedback (not static size-based)
    M = adaptive_modulation.adapt_m(
        message_size=len(msg_bytes),
        sinr_db=sinr_db,
        jammed_recently=jammed_recently,
        force_robust=(assessment.recommended_action == "ROBUST")
    )

    HYBRID_ASSESSMENTS.append({
        'ml_prob': float(assessment.ml_threat_probability),
        'threat_level': assessment.unified_threat_level.name,
        'action': assessment.recommended_action,
        'recommended_m': M,
        'agreement': float(assessment.agreement_score),
        'sinr_db': sinr_db,
        'ts': time.time(),
    })

    return assessment, assessment.recommended_action, M

def send_message(sock, recipient, message_text):
    try:
        nc, cp = 64, 8
        msg_bytes = message_text.encode("utf-8") if isinstance(message_text, str) else message_text
        is_control = message_text in [CTL_CONNECT_REQUEST, CTL_CONNECT_ACCEPT, CTL_CONNECT_REJECT, CTL_DISCONNECT, CTL_RETRANSMIT]

        # --- ANTI-JAM STRATEGY 1 & 2: Listen Before Talk + Adaptive M ---
        M = 16  # Default robust fallback
        if not is_control:
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                assessment, action, M = _assess_channel(msg_bytes, nc, cp)

                if action == "WAIT":
                    ANTI_JAM_STATS['deferred'] += 1
                    if attempt < max_attempts:
                        print(f"[ANTI-JAM] Channel JAMMED. Deferring transmission (attempt {attempt}/{max_attempts})...")
                        time.sleep(1.5)  # Wait for jammer cooldown
                        continue
                    else:
                        print(f"[ANTI-JAM] Channel still jammed after {max_attempts} attempts. Forcing robust M=16.")
                        M = 16
                        ANTI_JAM_STATS['robust_downgrades'] += 1
                        break
                elif action == "ROBUST":
                    ANTI_JAM_STATS['robust_downgrades'] += 1
                    print(f"[ANTI-JAM] Moderate threat detected. Using robust QAM-{M}.")
                    break
                else:  # TRANSMIT
                    print(f"[ANTI-JAM] Channel clear. Using QAM-{M} (Threat={assessment.unified_threat_level.name if assessment else 'N/A'})")
                    break

        # Build and encrypt
        energy, ofdm_signal = compute_ofdm_energy(msg_bytes, M, nc, cp)
        pkt = struct.pack(">H", len(msg_bytes)) + msg_bytes
        plaintext = struct.pack(">H H B", M, nc, cp) + pkt
        enc = aes_gcm_encrypt(plaintext, KEY)
        enc_rs = rs.encode(enc)

        # Transmit
        dst_b = recipient.encode("utf-8")
        payload = struct.pack(">H", len(dst_b)) + dst_b + struct.pack(">d", float(energy)) + enc_rs
        sock.sendall(struct.pack(">I", len(payload)) + payload)

        if not is_control:
            record_message(recipient, msg_bytes, M, message_text)
            adaptive_modulation.log_frame_result(success=True, jammed=False)
            LAST_SENT_MESSAGE[recipient] = message_text  # Save for retransmission
        return True
    except Exception as e:
        print(f"[ERROR] Send failed: {e}")
        adaptive_modulation.log_frame_result(success=False, jammed=False)
        return False

def receive_handler(sock):
    global chat_partner, connection_accepted
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

            try: plaintext = aes_gcm_decrypt(rs.decode(data[pos:])[0], KEY)
            except: continue

            pkt = plaintext[5:]; msg_len = struct.unpack(">H", pkt[:2])[0]
            msg = pkt[2:2+msg_len].decode("utf-8")

            if msg == CTL_CONNECT_ACCEPT: connection_status.set(); connection_accepted = True; continue
            if msg == CTL_CONNECT_REJECT: connection_status.set(); connection_accepted = False; continue
            if msg == CTL_DISCONNECT:
                with state_lock:
                    if src_id == chat_partner: print(f"\n[INFO] {chat_partner} disconnected.\n> ", end="", flush=True); chat_partner = None
                continue

            # --- ANTI-JAM: Handle retransmission request from receiver ---
            if msg == CTL_RETRANSMIT:
                ANTI_JAM_STATS['retransmissions'] += 1
                last_msg = LAST_SENT_MESSAGE.get(src_id)
                if last_msg:
                    print(f"\n[ANTI-JAM] Retransmit request from {src_id}. Resending: '{last_msg[:30]}...'\n> ", end="", flush=True)
                    send_message(sock, src_id, last_msg)
                else:
                    print(f"\n[ANTI-JAM] Retransmit request from {src_id} but no message to resend.\n> ", end="", flush=True)
                continue

            with state_lock:
                if src_id == chat_partner: print(f"\n<<< {src_id}: '{msg}'\n> ", end="", flush=True)
        except socket.timeout: continue
        except: continue

def send_handler(sock, node_id):
    global chat_partner
    while not stop_event.is_set():
        with state_lock: in_chat = chat_partner is not None
        if in_chat:
            message = input(f"> ")
            if message.strip().lower() == 'exit':
                send_message(sock, chat_partner, CTL_DISCONNECT)
                with state_lock: chat_partner = None; continue
            send_message(sock, chat_partner, message)
        else:
            print("\n" + "="*30)
            recipient = input("Enter recipient ID (or 'exit'): ").strip().upper()
            if recipient == 'EXIT': stop_event.set(); break

            send_message(sock, recipient, CTL_CONNECT_REQUEST)
            print(f"[INFO] Connecting to {recipient}...")
            connection_status.clear()
            if connection_status.wait(timeout=60.0):
                if connection_accepted:
                    with state_lock: chat_partner = recipient
                    print(f"[SUCCESS] Connected to {recipient}!")
                else: print("[INFO] Rejected.")
            else: print("[INFO] No response.")

def main():
    global NODE_ID
    node_id = ""
    while not node_id:
        u = input(f"Enter ID (S1-S60 or {', '.join(PRIMARY_SENDERS)}): ").strip().upper()
        if re.match(r"^S([1-9]|[1-5][0-9]|60)$", u) or u in PRIMARY_SENDERS: node_id = u
    NODE_ID = node_id

    cleanup_old_files(node_id)
    if node_id in PRIMARY_SENDERS: BS_HOST, BS_PORT, pos, typ = BS1_HOST, BS1_PORT, (0,0), "primary_sender"
    else: BS_HOST, BS_PORT, pos, typ = BS1_HOST, BS1_PORT, (100,0), "sender"

    try:
        sock = socket.create_connection((BS_HOST, BS_PORT), timeout=5)
        sock.sendall((json.dumps({"type": typ, "id": node_id, "pos": list(pos)}) + "\n").encode("utf-8"))
        print(f"\n{'='*70}")
        print(f"ML-HYBRID SENDER {node_id} (socket-based)")
        print(f"Using hybrid_anti_jamming_manager for channel assessment")
        print(f"Registered with BS at {BS_HOST}:{BS_PORT}")
        print(f"{'='*70}\n")
    except Exception as e:
        print(f"[ERROR] Could not connect to BS: {e}")
        return

    threading.Thread(target=receive_handler, args=(sock,), daemon=True).start()
    try: send_handler(sock, node_id)
    except: pass
    finally: stop_event.set(); export_all_results(node_id); sock.close()

if __name__ == "__main__":
    main()
