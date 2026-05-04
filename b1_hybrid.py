# b1_hybrid.py - ML-Hybrid Base Station (Socket-Based)
# Full networking from b1M.py + unified hybrid_anti_jamming_manager
# Replaces individual ML_RUNTIME calls with hybrid_manager.assess_packet()

import socket, threading, json, struct, time, os, math, glob, random, signal
from collections import deque
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from reedsolo import RSCodec
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
import numpy as np

# Import hybrid manager (combines all 4 anti-jamming techniques)
from hybrid_anti_jamming_manager import hybrid_manager, ThreatLevel

# --- CONFIGURATION ---
BS_INSTANCE = 1
ML_THREAT_THRESHOLD = 0.5
ML_ENFORCE_BLOCKING = False

RESULTS_DIR = "results"
os.makedirs(RESULTS_DIR, exist_ok=True)
HOST = "127.0.0.1"
COMM_RANGE = 150.0

if BS_INSTANCE == 1:
    PORT = 50050; BS_ID = "BS1"; BS_POS = (0.0, 0.0)
    NEIGHBORS = [("127.0.0.1", 50051)]
else:
    PORT = 50051; BS_ID = "BS2"; BS_POS = (200.0, 0.0)
    NEIGHBORS = [("127.0.0.1", 50050)]

# ---------- Crypto ----------
PASSPHRASE = b"very secret passphrase - change this!"
SALT = b'\x00'*16
KEY = PBKDF2(PASSPHRASE, SALT, dkLen=32, count=100000)
rs = RSCodec(40)
rs_robust = RSCodec(80)  # Strategy 3: Doubled RS redundancy during jamming

# ---------- State ----------
clients_lock = threading.Lock()
clients = {}
retry_queue = deque()
retry_queue_lock = threading.Lock()
stats = {"received": 0, "forwarded_local": 0, "forwarded_remote": 0, "queued": 0, "delivered": 0, "jammed": 0}
stats_lock = threading.Lock()
STOP = threading.Event()
SERVER_SOCKET = None

PRIMARY_SENDERS = ["JAZZ", "UFONE", "TELENOR", "WARID", "STARLINK", "ZONG", "SCO", "PTCL"]
ENERGY_HISTORY = []
THREAT_HISTORY = []
THREAT_HISTORY_LOCK = threading.Lock()
LAST_PRIMARY_ACTIVITY = 0.0
PRIMARY_PROTECTION_WINDOW = 10.0

# --- ANTI-JAM STATE ---
JAMMING_ACTIVE = False
JAMMING_LAST_SEEN = 0.0
JAMMING_TIMEOUT = 0.5

# Strategy 4: Store-and-Forward buffer for clean copies during jamming
jamming_buffer = deque()
jamming_buffer_lock = threading.Lock()
ANTI_JAM_STATS = {'buffered': 0, 'recovered': 0, 'rs_upgrades': 0}

def distance(a, b): return math.hypot(a[0]-b[0], a[1]-b[1])

def recv_full(sock, length):
    data = b""
    while len(data) < length:
        more = sock.recv(length - len(data))
        if not more: raise ConnectionError("closed")
        data += more
    return data

def send_all_with_retry(conn, data):
    try: conn.sendall(data); return True
    except: return False

def submit_frame_task(src_id, dst_id, payload, hop_count=0):
    threading.Thread(
        target=process_incoming_frame,
        args=(src_id, dst_id, payload, hop_count),
        daemon=True
    ).start()

def request_shutdown(reason=None):
    global SERVER_SOCKET
    if reason: print(f"[BS {BS_ID}] {reason}")
    STOP.set()
    with clients_lock:
        for info in list(clients.values()):
            conn = info.get("conn")
            if not conn: continue
            try: conn.shutdown(socket.SHUT_RDWR)
            except: pass
            try: conn.close()
            except: pass
    if SERVER_SOCKET is not None:
        try: SERVER_SOCKET.close()
        except: pass
        SERVER_SOCKET = None

def _signal_handler(signum, _frame):
    request_shutdown(f"Shutdown signal received ({signum})")

# PHY Helpers
def aes_gcm_encrypt(plaintext, key):
    from Crypto.Random import get_random_bytes
    nonce = get_random_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return nonce + tag + ciphertext

def aes_gcm_decrypt(enc_blob, key):
    nonce, tag, ciphertext = enc_blob[:12], enc_blob[12:28], enc_blob[28:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag)

def split_optional_sensing_header(payload):
    if len(payload) < 8: return None, payload
    try: sensing_energy = struct.unpack(">d", payload[:8])[0]
    except struct.error: return None, payload
    if (not math.isfinite(sensing_energy)) or sensing_energy < 0.0 or sensing_energy > 10.0:
        return None, payload
    return float(sensing_energy), payload[8:]

def decode_payload_with_optional_header(payload):
    sensing_energy, stripped_payload = split_optional_sensing_header(payload)
    decode_candidates = [(stripped_payload, sensing_energy)] if sensing_energy is not None else []
    decode_candidates.append((payload, None))
    for encoded_payload, candidate_energy in decode_candidates:
        try:
            rs_decoded = rs.decode(encoded_payload)[0]
            plaintext = aes_gcm_decrypt(rs_decoded, KEY)
            return plaintext, candidate_energy
        except: continue
    return None, sensing_energy

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

def process_incoming_frame(src_id, dst_id, encoded_bytes, hop_count=0):
    global JAMMING_ACTIVE, JAMMING_LAST_SEEN

    # 0. JAMMING CHECK
    if time.time() - JAMMING_LAST_SEEN < JAMMING_TIMEOUT:
        JAMMING_ACTIVE = True
    else:
        JAMMING_ACTIVE = False

    final_payload = encoded_bytes
    if JAMMING_ACTIVE:
        print(f"[BS {BS_ID}] ! JAMMING ACTIVE ! Corrupting packet from {src_id}...")
        with stats_lock: stats["jammed"] += 1
        ba = bytearray(encoded_bytes)
        corruption_intensity = int(len(ba) * 0.3)
        for _ in range(corruption_intensity):
            idx = random.randint(0, len(ba)-1)
            ba[idx] = ba[idx] ^ random.randint(1, 255)
        final_payload = bytes(ba)

        # --- ANTI-JAM Strategy 4: Buffer the CLEAN copy for retransmission when jamming stops ---
        with jamming_buffer_lock:
            jamming_buffer.append({"src": src_id, "dst": dst_id, "data": encoded_bytes, "hop": hop_count, "ts": time.time()})
            ANTI_JAM_STATS['buffered'] += 1
        print(f"[BS {BS_ID}] [ANTI-JAM] Buffered clean copy of {src_id}->{dst_id} for recovery")

    try:
        # 1. Decode + ML scoring via HYBRID MANAGER
        plaintext, sensing_energy = decode_payload_with_optional_header(final_payload)
        energy = float(sensing_energy) if sensing_energy is not None else 0.0
        ofdm_sig = np.array([])

        if plaintext is not None:
            try:
                M, nc, cp = struct.unpack(">H H B", plaintext[:5])
                packet = plaintext[5:]
                msg_len = struct.unpack(">H", packet[:2])[0]
                msg_bytes = packet[2:2 + msg_len]
                tx_syms = qam_mod(bits_from_bytes(msg_bytes), M)
                ofdm_sig = ofdm_mod(tx_syms, nc, cp)
                if ofdm_sig.size > 0:
                    energy = float(np.mean(np.abs(ofdm_sig)**2))
            except (struct.error, ValueError): pass

        if energy > 0.0:
            ENERGY_HISTORY.append((time.time(), src_id, energy))

        # --- HYBRID THREAT ASSESSMENT ---
        if ofdm_sig.size == 0:
            ofdm_sig = np.random.normal(0, 1e-6, 256) + 1j*np.random.normal(0, 1e-6, 256)

        assessment = hybrid_manager.assess_packet(
            ofdm_signal=ofdm_sig,
            sensing_energy=sensing_energy if sensing_energy is not None else energy,
        )

        with stats_lock:
            stats.setdefault("ml_scored", 0)
            stats.setdefault("ml_alerts", 0)
            stats.setdefault("ml_blocked", 0)
            stats["ml_scored"] += 1
            if assessment.unified_threat_level in [ThreatLevel.HIGH, ThreatLevel.CRITICAL]:
                stats["ml_alerts"] += 1

        with THREAT_HISTORY_LOCK:
            THREAT_HISTORY.append({
                'time': time.time(),
                'src': src_id,
                'ml_prob': float(assessment.ml_threat_probability),
                'threat_level': assessment.unified_threat_level.name,
                'confidence': float(assessment.unified_confidence),
                'agreement': float(assessment.agreement_score),
                'spectrum_state': str(assessment.spectrum_state),
                'recommended_m': assessment.recommended_m,
                'action': assessment.recommended_action,
            })

        if assessment.unified_threat_level in [ThreatLevel.HIGH, ThreatLevel.CRITICAL]:
            print(
                f"[BS {BS_ID}] [HYBRID-THREAT] {src_id}->{dst_id} "
                f"Level={assessment.unified_threat_level.name} "
                f"ML={assessment.ml_threat_probability:.3f} "
                f"Agreement={assessment.agreement_score:.2f} "
                f"Action={assessment.recommended_action}"
            )
            if ML_ENFORCE_BLOCKING and src_id.upper() not in PRIMARY_SENDERS:
                print(f"[BS {BS_ID}] [HYBRID-THREAT] Blocking packet from {src_id}.")
                with stats_lock: stats["ml_blocked"] += 1
                return

        if src_id.upper() in PRIMARY_SENDERS and energy > 0.0:
            global LAST_PRIMARY_ACTIVITY
            LAST_PRIMARY_ACTIVITY = time.time()
            print(f"[BS {BS_ID}] Primary Activity: {src_id} (E={energy:.2e})")

        # 2. Priority Logic
        is_primary_link = (src_id.upper() in PRIMARY_SENDERS) or (dst_id.upper() in PRIMARY_SENDERS)
        if (not is_primary_link) and (time.time() - LAST_PRIMARY_ACTIVITY < PRIMARY_PROTECTION_WINDOW):
            print(f"[BS {BS_ID}] Deferring Secondary {src_id}->{dst_id} (Channel protected)")
            with stats_lock: stats["queued"] += 1
            with retry_queue_lock:
                retry_queue.append({"src": src_id, "dst": dst_id, "data": final_payload, "hop": hop_count, "ts": time.time()})
            return

        # --- ANTI-JAM Strategy 3: Use RS-80 for outgoing delivery during HIGH threat ---
        delivery_payload = final_payload
        if assessment.unified_threat_level in [ThreatLevel.HIGH, ThreatLevel.CRITICAL] and plaintext is not None:
            try:
                enc_blob = aes_gcm_encrypt(plaintext, KEY)
                delivery_payload_rs80 = rs_robust.encode(enc_blob)
                # Prepend sensing energy header if present
                if sensing_energy is not None:
                    delivery_payload = struct.pack(">d", float(sensing_energy)) + delivery_payload_rs80
                else:
                    delivery_payload = delivery_payload_rs80
                ANTI_JAM_STATS['rs_upgrades'] += 1
                print(f"[BS {BS_ID}] [ANTI-JAM] RS upgraded to RS-80 for {src_id}->{dst_id}")
            except Exception as e:
                print(f"[BS {BS_ID}] [ANTI-JAM] RS upgrade failed: {e}")

        # 3. Routing
        with clients_lock: dst_info = clients.get(dst_id)
        if dst_info and distance(BS_POS, dst_info["pos"]) <= COMM_RANGE:
            src_b = src_id.encode("utf-8")
            payload = struct.pack(">H", len(src_b)) + src_b + delivery_payload
            if send_all_with_retry(dst_info["conn"], struct.pack(">I", len(payload)) + payload):
                print(f"[BS {BS_ID}] Delivered: {src_id} -> {dst_id}")
                with stats_lock: stats["delivered"] += 1; stats["forwarded_local"] += 1
                return

        src_b, dst_b = src_id.encode("utf-8"), dst_id.encode("utf-8")
        hop_payload = struct.pack(">B H", hop_count+1, len(src_b)) + src_b + struct.pack(">H", len(dst_b)) + dst_b + delivery_payload
        for nb_h, nb_p in NEIGHBORS:
            try:
                with socket.create_connection((nb_h, nb_p), timeout=2) as s:
                    s.sendall(struct.pack(">I", len(hop_payload)) + hop_payload)
                    print(f"[BS {BS_ID}] Hopped: {src_id} -> {dst_id}")
                    with stats_lock: stats["forwarded_remote"] += 1
                    return
            except: continue

        print(f"[BS {BS_ID}] Queueing {dst_id} (Unreachable)")
        with retry_queue_lock:
            retry_queue.append({"src": src_id, "dst": dst_id, "data": final_payload, "hop": hop_count, "ts": time.time()})

    except Exception as e: print(f"[BS {BS_ID}] Error: {e}")

def handle_registered_client(conn, addr):
    client_id = None
    try:
        buf = b""
        while b"\n" not in buf: buf += conn.recv(1024)
        line, buf = buf.split(b"\n", 1)
        reg = json.loads(line.decode("utf-8"))
        client_id = reg["id"]
        client_type = reg.get("type", "sender")

        # --- JAMMER HANDLING ---
        if client_type == "jammer":
            print(f"[BS {BS_ID}] !!! WARNING: JAMMER CONNECTED ({client_id}) !!!")
            while not STOP.is_set():
                h = recv_full(conn, 4)
                length = struct.unpack(">I", h)[0]
                _ = recv_full(conn, length)
                global JAMMING_LAST_SEEN
                JAMMING_LAST_SEEN = time.time()
            return

        with clients_lock: clients[client_id] = {"conn": conn, "pos": tuple(reg["pos"])}
        print(f"[BS {BS_ID}] Registered {client_id}")

        while not STOP.is_set():
            while len(buf) < 4: buf += conn.recv(4096)
            length, buf = struct.unpack(">I", buf[:4])[0], buf[4:]
            while len(buf) < length: buf += conn.recv(4096)
            payload, buf = buf[:length], buf[length:]
            dst_len = struct.unpack(">H", payload[:2])[0]
            dst_id = payload[2:2+dst_len].decode("utf-8")
            remaining = payload[2+dst_len:]
            with stats_lock: stats["received"] += 1
            submit_frame_task(client_id, dst_id, remaining)
    except: pass
    finally:
        with clients_lock: clients.pop(client_id, None) if client_id else None
        conn.close()

def handle_hop_connection(conn, addr):
    try:
        payload = recv_full(conn, struct.unpack(">I", recv_full(conn, 4))[0])
        hop, pos = payload[0], 1
        src_len = struct.unpack(">H", payload[pos:pos+2])[0]; pos += 2
        src = payload[pos:pos+src_len].decode("utf-8"); pos += src_len
        dst_len = struct.unpack(">H", payload[pos:pos+2])[0]; pos += 2
        dst = payload[pos:pos+dst_len].decode("utf-8"); pos += dst_len
        submit_frame_task(src, dst, payload[pos:], hop)
    except: pass
    finally: conn.close()

def retry_worker():
    while not STOP.is_set():
        time.sleep(1.0)
        if time.time() - LAST_PRIMARY_ACTIVITY < PRIMARY_PROTECTION_WINDOW: continue
        with retry_queue_lock:
            for _ in range(len(retry_queue)):
                item = retry_queue.popleft()
                submit_frame_task(item["src"], item["dst"], item["data"], item["hop"])

def jamming_recovery_worker():
    """Strategy 4: Flush buffered clean packets when jamming stops."""
    while not STOP.is_set():
        time.sleep(1.0)
        if JAMMING_ACTIVE:
            continue  # Still under attack, keep buffering
        # Jamming stopped — flush buffer
        with jamming_buffer_lock:
            count = len(jamming_buffer)
            if count == 0:
                continue
            print(f"\n[BS {BS_ID}] [ANTI-JAM] Jamming stopped! Flushing {count} buffered packets...")
            for _ in range(count):
                item = jamming_buffer.popleft()
                ANTI_JAM_STATS['recovered'] += 1
                submit_frame_task(item["src"], item["dst"], item["data"], item["hop"])
            print(f"[BS {BS_ID}] [ANTI-JAM] Recovery complete. {count} packets re-delivered.")

def export_base_station_analysis():
    """Generate BS analysis plots including hybrid threat data"""
    print(f"\n[BS {BS_ID}] Generating hybrid analysis plots...")
    figs = []

    # Plot 1: Energy Timeline
    if ENERGY_HISTORY:
        fig = plt.figure(figsize=(12,5))
        times = [(e[0] - ENERGY_HISTORY[0][0]) for e in ENERGY_HISTORY]
        energies = [e[2] for e in ENERGY_HISTORY]
        sources = [e[1] for e in ENERGY_HISTORY]
        colors = ['red' if s.upper() in PRIMARY_SENDERS else 'blue' for s in sources]
        plt.scatter(times, energies, c=colors, s=50, alpha=0.6)
        plt.title(f"[BS {BS_ID}] Energy Timeline (Red=Primary, Blue=Secondary)")
        plt.xlabel("Time (s)"); plt.ylabel("Energy (W)"); plt.grid(True, alpha=0.3)
        figs.append(fig)

    # Plot 2: Statistics Summary
    fig = plt.figure(figsize=(10,6))
    stat_labels = list(stats.keys())
    stat_values = [stats[k] for k in stat_labels]
    colors_stat = plt.cm.tab20(np.linspace(0, 1, len(stat_labels)))
    plt.bar(stat_labels, stat_values, color=colors_stat, alpha=0.7, edgecolor='black')
    plt.title(f"[BS {BS_ID}] Session Statistics"); plt.ylabel("Count"); plt.xticks(rotation=45)
    for i, v in enumerate(stat_values):
        plt.text(i, v + max(stat_values)*0.02, str(v), ha='center', fontweight='bold')
    plt.grid(True, alpha=0.3, axis='y')
    figs.append(fig)

    # Plot 3: Hybrid Threat Timeline
    with THREAT_HISTORY_LOCK:
        threat_history = list(THREAT_HISTORY)
    if threat_history:
        fig, axes = plt.subplots(3, 1, figsize=(14, 10))
        fig.suptitle(f'[BS {BS_ID}] Hybrid Threat Detection Timeline', fontsize=16, fontweight='bold')

        ml_probs = [t['ml_prob'] for t in threat_history]
        confidences = [t['confidence'] for t in threat_history]
        agreements = [t['agreement'] for t in threat_history]
        x = list(range(len(threat_history)))

        axes[0].plot(x, ml_probs, 'r-', linewidth=2, label='ML Threat Prob')
        axes[0].axhline(y=ML_THREAT_THRESHOLD, color='orange', linestyle='--', label='Threshold')
        axes[0].fill_between(x, 0, ml_probs, alpha=0.3, color='red')
        axes[0].set_ylabel('Threat Probability'); axes[0].legend(); axes[0].grid(True, alpha=0.3)

        axes[1].plot(x, confidences, 'b-', linewidth=2, label='Unified Confidence')
        axes[1].plot(x, agreements, 'g--', linewidth=2, label='Agreement Score')
        axes[1].set_ylabel('Score (0-1)'); axes[1].legend(); axes[1].grid(True, alpha=0.3)

        level_map = {'NONE': 0, 'LOW': 1, 'MEDIUM': 2, 'HIGH': 3, 'CRITICAL': 4}
        level_vals = [level_map.get(t['threat_level'], 0) for t in threat_history]
        level_colors = ['green' if v==0 else 'yellow' if v==1 else 'orange' if v==2 else 'red' if v==3 else 'darkred' for v in level_vals]
        axes[2].scatter(x, level_vals, c=level_colors, s=50, alpha=0.7)
        axes[2].set_yticks([0,1,2,3,4]); axes[2].set_yticklabels(['NONE','LOW','MEDIUM','HIGH','CRITICAL'])
        axes[2].set_xlabel('Frame #'); axes[2].grid(True, alpha=0.3, axis='y')

        plt.tight_layout()
        figs.append(fig)

    # Plot 4: Spectrum Utilization
    if ENERGY_HISTORY:
        fig = plt.figure(figsize=(10,6))
        legit = sum(1 for _, src, _ in ENERGY_HISTORY if src != "JAMMER_01")
        vals = [legit, stats.get('delivered',0), stats.get('jammed',0), stats.get('queued',0)]
        labels = ['Transmitted', 'Delivered', 'Jammed', 'Queued']
        colors_eff = ['green', 'darkgreen', 'red', 'orange']
        bars = plt.bar(labels, vals, color=colors_eff, alpha=0.7, edgecolor='black')
        for bar, val in zip(bars, vals):
            plt.text(bar.get_x() + bar.get_width()/2., bar.get_height(), f'{val}', ha='center', va='bottom', fontweight='bold')
        plt.title(f"[BS {BS_ID}] Spectrum Utilization"); plt.ylabel("Count"); plt.grid(True, alpha=0.3, axis='y')
        figs.append(fig)

    # Save
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    pdf_path = os.path.join(RESULTS_DIR, f"{BS_ID}_hybrid_analysis_{timestamp}.pdf")
    try:
        with PdfPages(pdf_path) as pdf:
            for f in figs: pdf.savefig(f); plt.close(f)
        print(f"[BS {BS_ID}] Saved {len(figs)} plots to {pdf_path}")
    except Exception as e:
        print(f"[BS {BS_ID}] Error saving plots: {e}")

    # Save hybrid results JSON
    json_path = os.path.join(RESULTS_DIR, f"{BS_ID}_hybrid_results_{timestamp}.json")
    try:
        diag = hybrid_manager.get_diagnostics()
        results = {
            'implementation': 'ML-Hybrid Base Station (socket-based)',
            'stats': dict(stats),
            'hybrid_diagnostics': {k: v for k, v in diag.items() if not callable(v)},
            'threat_events': len(threat_history),
        }
        with open(json_path, 'w') as f: json.dump(results, f, indent=2)
        print(f"[BS {BS_ID}] Saved hybrid results to {json_path}")
    except Exception as e:
        print(f"[BS {BS_ID}] Error saving JSON: {e}")

def main():
    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal_handler)

    threading.Thread(target=retry_worker, daemon=True).start()
    threading.Thread(target=jamming_recovery_worker, daemon=True).start()

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            global SERVER_SOCKET
            SERVER_SOCKET = sock
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((HOST, PORT))
            sock.listen(16)
            sock.settimeout(1.0)
            print(f"\n{'='*70}")
            print(f"ML-HYBRID BASE STATION {BS_ID} (socket-based)")
            print(f"Combining: RF/XGBoost + Spectrum Sensing + Jammer Detector + Adaptive M")
            print(f"Anti-Jamming: Adaptive RS + Store-and-Forward Recovery")
            print(f"Listening on {HOST}:{PORT}")
            print(f"{'='*70}\n")

            while not STOP.is_set():
                try:
                    conn, addr = sock.accept()
                except socket.timeout: continue
                except OSError:
                    if STOP.is_set(): break
                    raise

                try: first_byte = conn.recv(1, socket.MSG_PEEK)
                except OSError: conn.close(); continue

                if first_byte == b'{':
                    threading.Thread(target=handle_registered_client, args=(conn, addr), daemon=True).start()
                else:
                    threading.Thread(target=handle_hop_connection, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        request_shutdown("KeyboardInterrupt received")
    finally:
        request_shutdown("Shutting down...")
        time.sleep(0.3)
        export_base_station_analysis()

if __name__ == "__main__":
    main()
