import socket
import sys
import select
import json
import argparse
import time
import base64
from PIL import Image, ImageTk
import io
import math
from utils import get_interface_ip
from overlay_structs import OverlayNode, MsgType, MAX_PACKET_SIZE

try:
    import tkinter as tk
    from tkinter import Label
    HAS_GUI = True
except ImportError:
    HAS_GUI = False

DEFAULT_PORT = 50000
BOOTSTRAP_PORT = 6000
VIDEO_FILE = "movie.Mjpeg" 

# --- MOTOR DE CORREÇÃO DE ERROS (FEC XOR) ---
class FECEngine:
    @staticmethod
    def create_parity_packet(chunks_b64):
        if not chunks_b64: return None
        max_len = max(len(c) for c in chunks_b64)
        parity_bytes = bytearray(max_len)
        for chunk in chunks_b64:
            chunk_bytes = chunk.encode('utf-8')
            for i in range(len(chunk_bytes)):
                parity_bytes[i] ^= chunk_bytes[i]
        return base64.b64encode(parity_bytes).decode('utf-8')

    @staticmethod
    def recover_missing_chunk(existing_chunks, parity_b64, missing_idx, max_len):
        parity_bytes = bytearray(base64.b64decode(parity_b64))
        recovered_bytes = bytearray(len(parity_bytes))
        recovered_bytes[:] = parity_bytes[:]
        for chunk in existing_chunks:
            if chunk is None: continue 
            chunk_bytes = chunk.encode('utf-8')
            for i in range(len(chunk_bytes)):
                recovered_bytes[i] ^= chunk_bytes[i]
        return base64.b64encode(recovered_bytes[:max_len]).decode('utf-8')

class VideoStream:
    def __init__(self, filename):
        self.filename = filename
        try:
            self.file = open(filename, 'rb')
            print(f"[STREAM] Ficheiro {filename} aberto com sucesso.")
        except:
            print(f"[ERRO] Não encontrei o ficheiro {filename}.")
            self.file = None
        self.frame_num = 0

    def next_frame(self):
        if not self.file: return None
        try:
            data = self.file.read(5)
            if not data: 
                self.file.seek(0) 
                data = self.file.read(5)
            framelength = int(data)
            data = self.file.read(framelength)
            self.frame_num += 1
            return data
        except: return None

class VideoGUI:
    def __init__(self, root, title):
        self.root = root
        self.root.title(title)
        self.label = Label(root)
        self.label.pack()
        self.root.update()

    def update_image(self, data_bytes):
        try:
            image = tk.PhotoImage(data=data_bytes) 
            self.label.configure(image=image)
            self.label.image = image
            self.root.update()
        except Exception as e:
            pass

def get_neighbors_dynamic(tracker_ip, my_id, my_ip):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3.0) 
        sock.connect((tracker_ip, BOOTSTRAP_PORT))
        request = json.dumps({"id": my_id, "ip": my_ip})
        sock.send(request.encode('utf-8'))
        data = sock.recv(4096)
        response = json.loads(data.decode('utf-8'))
        sock.close()
        
        if response.get("status") == "OK":
            neighbors = response.get("neighbors", [])
            print(f"[*] Tracker: Vizinhos atribuídos -> {neighbors}")
            return neighbors
        return []
    except Exception as e:
        print(f"[ERRO] Tracker offline: {e}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('node_id')
    parser.add_argument('--tracker', help='IP do Tracker', required=True)
    args = parser.parse_args()

    my_ip = get_interface_ip()
    print(f"[*] Nó {args.node_id} ({my_ip}) ONLINE")

    initial_neighbors = get_neighbors_dynamic(args.tracker, args.node_id, my_ip)
    
    node = OverlayNode(args.node_id, my_ip, DEFAULT_PORT)
    for neighbor_ip in initial_neighbors:
        node.neighbors[neighbor_ip] = {'metric': 50.0, 'last_seen': 0}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', DEFAULT_PORT))
    sock.setblocking(0)

    video_stream = None
    gui = None
    if "STREAMER" in args.node_id: video_stream = VideoStream(VIDEO_FILE)
    if "C" in args.node_id and HAS_GUI:
        try:
            root = tk.Tk()
            gui = VideoGUI(root, f"Cliente {args.node_id}")
        except Exception as e:
            print(f"\n[ERRO CRÍTICO GUI] Não consigo abrir a janela: {e}")
            print("[DICA] Tente fazer 'export DISPLAY=:0' no terminal antes de rodar.")

    reassembly_buffer = {} 
    
    join_state = {
        'active': False, 
        'stream_id': None, 
        'target_ip': None, 
        'last_sent': 0, 
        'retries': 0
    }
    
    last_hello = 0
    last_flood = 0
    last_frame = 0
    last_report = 0
    
    HELLO_INTERVAL = 1.0 
    FLOOD_INTERVAL = 10.0 
    
    # --- CONTROLO DE CONGESTIONAMENTO ---
    FRAME_INTERVAL = 0.50 # Começa a 20 FPS (0.05s)
    MIN_INTERVAL = 0.03   # Max ~30 FPS
    MAX_INTERVAL = 0.5    # Min ~2 FPS
    
    CHUNK_SIZE = 1024 
    JOIN_TIMEOUT = 5.0
    REPORT_INTERVAL = 5.0
    
    stats_frames_received = 0
    stats_frames_lost = 0

    inputs = [sock, sys.stdin]
    is_streamer = "STREAMER" in args.node_id
    frame_seq = 0

    print("[*] Sistema pronto. Comandos: 'join <STREAM_ID>', 'leave <STREAM_ID>', 'status'.")
    
    try:
        while True:
            if gui:
                try:
                    gui.root.update_idletasks()
                    gui.root.update()
                except: pass

            now = time.time()
            
            # --- 1. Retransmissão de JOIN ---
            if join_state['active']:
                if now - join_state['last_sent'] > JOIN_TIMEOUT:
                    if join_state['retries'] < 3: 
                        print(f"[⏳] Timeout. Reenviando JOIN para {join_state['target_ip']}...")
                        pl = json.dumps({"stream_id": join_state['stream_id']}).encode('utf-8')
                        pk = node.pack_message(MsgType.STREAM_JOIN, join_state['target_ip'], pl)
                        sock.sendto(pk, (join_state['target_ip'], DEFAULT_PORT))
                        join_state['last_sent'] = now
                        join_state['retries'] += 1
                    else:
                        print(f"[❌] Falha no JOIN: Vizinho não responde.")
                        join_state['active'] = False

            # --- 2. Hellos ---
            if now - last_hello >= HELLO_INTERVAL:
                for n_ip in node.neighbors:
                    pkt = node.pack_message(MsgType.HELLO, n_ip, b"")
                    node.pending_pings[node.sequence_number] = now
                    sock.sendto(pkt, (n_ip, DEFAULT_PORT))
                last_hello = now

            # --- 3. Flood ---
            if is_streamer and now - last_flood >= FLOOD_INTERVAL:
                flood_payload = json.dumps({
                    "stream_id": args.node_id,
                    "cost": 0,
                    "origin_seq": int(now)
                }).encode('utf-8')
                print(f"[📢] A iniciar Flood (Seq {int(now)})...") 
                for n_ip in node.neighbors:
                    pkt = node.pack_message(MsgType.ROUTE_DISCOVERY, n_ip, flood_payload)
                    sock.sendto(pkt, (n_ip, DEFAULT_PORT))
                last_flood = now

            # --- 4. QoS Reports (Cliente) ---
            if "C" in args.node_id and now - last_report >= REPORT_INTERVAL:
                if stats_frames_received > 0 or stats_frames_lost > 0:
                    total = stats_frames_received + stats_frames_lost
                    loss_rate = (stats_frames_lost / total) * 100.0 if total > 0 else 0
                    if join_state['stream_id'] and join_state['target_ip']:
                         report_payload = json.dumps({
                             "stream_id": join_state['stream_id'], "client_id": args.node_id, "loss_rate": loss_rate
                         }).encode('utf-8')
                         pkt = node.pack_message(MsgType.STREAM_REPORT, join_state['target_ip'], report_payload)
                         sock.sendto(pkt, (join_state['target_ip'], DEFAULT_PORT))
                         stats_frames_received = 0
                         stats_frames_lost = 0
                last_report = now

            # --- 5. Vídeo (Streamer) ---
            if is_streamer and video_stream and now - last_frame >= FRAME_INTERVAL:
                if args.node_id in node.routing_table:
                    clients = node.routing_table[args.node_id].downstream_ips
                    if clients:
                        raw_bytes = video_stream.next_frame()
                        if raw_bytes:
                            frame_seq += 1
                            b64_data = base64.b64encode(raw_bytes).decode('utf-8')
                            num_chunks = math.ceil(len(b64_data) / CHUNK_SIZE)
                            chunks_list = []
                            for i in range(num_chunks):
                                chunk_data = b64_data[i*CHUNK_SIZE : (i+1)*CHUNK_SIZE]
                                chunks_list.append(chunk_data)
                                payload = json.dumps({
                                    "id": args.node_id, "fid": frame_seq, "cid": i, "tot": num_chunks, "data": chunk_data
                                }).encode('utf-8')
                                for client_ip in clients:
                                    pkt = node.pack_message(MsgType.STREAM_DATA, client_ip, payload)
                                    sock.sendto(pkt, (client_ip, DEFAULT_PORT))
                            
                            fec_data = FECEngine.create_parity_packet(chunks_list)
                            fec_payload = json.dumps({
                                "id": args.node_id, "fid": frame_seq, "tot": num_chunks, "data": fec_data
                            }).encode('utf-8')
                            for client_ip in clients:
                                pkt = node.pack_message(MsgType.STREAM_FEC, client_ip, fec_payload)
                                sock.sendto(pkt, (client_ip, DEFAULT_PORT))
                last_frame = now

            # --- EVENT LOOP ---
            readable, _, _ = select.select(inputs, [], [], 0.01)
            
            for s in readable:
                if s is sock:
                    try:
                        data, addr = sock.recvfrom(MAX_PACKET_SIZE)
                        sender_ip_real = addr[0]
                        
                        #if sender_ip_real not in node.neighbors:
                            # Filtro Estrito para demo segura (mas se preferir auto-discovery descomente)
                            # print(f"[AUTO] Novo vizinho detetado: {sender_ip_real}")
                            # node.neighbors[sender_ip_real] = {'metric': 50.0, 'last_seen': 0}
                            #continue # Ignora desconhecidos para forçar topologia do JSON

                        header, payload = node.unpack_message(data)
                        if not header: continue

                        if header['type'] == MsgType.HELLO:
                            resp = node.handle_hello(header, sender_ip_real)
                            pkt = node.pack_message(MsgType.HELLO_RESPONSE, sender_ip_real, resp)
                            sock.sendto(pkt, (sender_ip_real, DEFAULT_PORT))

                        elif header['type'] == MsgType.HELLO_RESPONSE:
                            node.handle_hello_response(payload, sender_ip_real)

                        elif header['type'] == MsgType.ROUTE_DISCOVERY:
                            new_payload = node.handle_flood(header, payload, sender_ip_real)
                            if new_payload:
                                for n_ip in node.neighbors:
                                    if n_ip != sender_ip_real:
                                        pkt = node.pack_message(MsgType.ROUTE_DISCOVERY, n_ip, new_payload)
                                        sock.sendto(pkt, (n_ip, DEFAULT_PORT))

                        elif header['type'] == MsgType.STREAM_JOIN:
                            # Antes de processar, verificar se já estamos a fornecer esta stream
                            try:
                                jdata_preview = json.loads(payload.decode('utf-8'))
                                sid_preview = jdata_preview.get('stream_id')
                            except:
                                sid_preview = None

                            had_downstream = False
                            if sid_preview and sid_preview in node.routing_table:
                                try:
                                    had_downstream = len(node.routing_table[sid_preview].downstream_ips) > 0
                                except: had_downstream = False

                            upstream_ip, send_ack = node.handle_join(payload, sender_ip_real)
                            if send_ack:
                                try:
                                    jdata = json.loads(payload.decode('utf-8'))
                                    sid = jdata['stream_id']
                                    ack_pl = json.dumps({"stream_id": sid}).encode('utf-8')
                                    ack_pkt = node.pack_message(MsgType.ACK_JOIN, sender_ip_real, ack_pl)
                                    sock.sendto(ack_pkt, (sender_ip_real, DEFAULT_PORT))
                                except: pass

                            # Se já tínhamos clientes para esta stream (i.e., já estamos a fornecer),
                            # não propagamos o JOIN upstream — passamos a servir o novo downstream.
                            already_serving = False
                            if sid_preview:
                                try:
                                    already_serving = had_downstream or (node.node_id == sid_preview)
                                except: already_serving = had_downstream

                            if upstream_ip and upstream_ip != "SOURCE" and not already_serving:
                                pkt = node.pack_message(MsgType.STREAM_JOIN, upstream_ip, payload)
                                sock.sendto(pkt, (upstream_ip, DEFAULT_PORT))
                                print(f"[⬆️] JOIN propagado -> {upstream_ip}")

                        elif header['type'] == MsgType.ACK_JOIN:
                            if join_state['active']:
                                print(f"[✅] ACK recebido de {sender_ip_real}! Ligação estabelecida.")
                                join_state['active'] = False 

                        elif header['type'] == MsgType.STREAM_LEAVE:
                            upstream_prune = node.handle_leave(payload, sender_ip_real)
                            if upstream_prune and upstream_prune != "SOURCE":
                                pkt = node.pack_message(MsgType.STREAM_LEAVE, upstream_prune, payload)
                                sock.sendto(pkt, (upstream_prune, DEFAULT_PORT))
                                print(f"[🚫] LEAVE -> {upstream_prune}")

                        elif header['type'] == MsgType.STREAM_REPORT:
                            upstream_report = node.handle_report(payload, sender_ip_real)
                            
                            # Se sou o Streamer, ajusto a velocidade!
                            if upstream_report == "SOURCE":
                                try:
                                    info = json.loads(payload.decode('utf-8'))
                                    loss = info.get('loss_rate', 0.0)
                                    client = info.get('client_id')
                                    
                                    # LÓGICA DE CONTROLO DE CONGESTIONAMENTO
                                    if loss > 10.0:
                                        FRAME_INTERVAL = min(FRAME_INTERVAL * 1.5, MAX_INTERVAL)
                                        print(f"[⚠️] Congestionamento ({loss:.1f}%). Reduzindo FPS para {1/FRAME_INTERVAL:.1f} Hz")
                                    elif loss < 2.0:
                                        FRAME_INTERVAL = max(FRAME_INTERVAL * 0.9, MIN_INTERVAL)
                                        # print(f"[🚀] Rede boa. Aumentando FPS para {1/FRAME_INTERVAL:.1f} Hz")
                                except: pass
                            
                            elif upstream_report:
                                pkt = node.pack_message(MsgType.STREAM_REPORT, upstream_report, payload)
                                sock.sendto(pkt, (upstream_report, DEFAULT_PORT))

                        elif header['type'] in [MsgType.STREAM_DATA, MsgType.STREAM_FEC]:
                            try:
                                info = json.loads(payload.decode('utf-8'))
                                s_id = info.get('id')
                                
                                if s_id in node.routing_table:
                                    for child in node.routing_table[s_id].downstream_ips:
                                        if child != sender_ip_real:
                                            pkt = node.pack_message(header['type'], child, payload)
                                            sock.sendto(pkt, (child, DEFAULT_PORT))
                                
                                if "C" in args.node_id:
                                    fid = info.get('fid')
                                    tot = info.get('tot')
                                    chunk_data = info.get('data')

                                    if fid not in reassembly_buffer:
                                        reassembly_buffer[fid] = {'chunks': {}, 'fec': None, 'tot': tot}
                                    
                                    entry = reassembly_buffer[fid]
                                    
                                    if header['type'] == MsgType.STREAM_DATA:
                                        cid = info.get('cid')
                                        entry['chunks'][cid] = chunk_data
                                    elif header['type'] == MsgType.STREAM_FEC:
                                        entry['fec'] = chunk_data

                                    received_count = len(entry['chunks'])
                                    
                                    success = False
                                    if received_count == tot:
                                        full = "".join([entry['chunks'][i] for i in range(tot)])
                                        ibytes = base64.b64decode(full)
                                        print(f"\r[📺] Frame {fid} OK ({len(ibytes)}b)", end="")
                                        sys.stdout.flush()
                                        if gui: gui.update_image(ibytes)
                                        success = True
                                        del reassembly_buffer[fid]

                                    elif received_count == tot - 1 and entry['fec']:
                                        missing_cid = -1
                                        chunks_for_recovery = []
                                        max_len = 0
                                        for i in range(tot):
                                            if i in entry['chunks']:
                                                c = entry['chunks'][i]
                                                chunks_for_recovery.append(c)
                                                max_len = max(max_len, len(c))
                                            else:
                                                missing_cid = i
                                                chunks_for_recovery.append(None)
                                        
                                        recovered_chunk = FECEngine.recover_missing_chunk(
                                            chunks_for_recovery, entry['fec'], missing_cid, max_len
                                        )
                                        entry['chunks'][missing_cid] = recovered_chunk
                                        full = "".join([entry['chunks'][i] for i in range(tot)])
                                        try:
                                            ibytes = base64.b64decode(full)
                                            if gui: gui.update_image(ibytes)
                                            print(f"\r[✨] Frame {fid} RECUPERADO!", end="")
                                            sys.stdout.flush()
                                            success = True
                                        except: pass
                                        del reassembly_buffer[fid]

                                    if success: stats_frames_received += 1
                                    
                                    for k in list(reassembly_buffer): 
                                        if k < fid - 10: 
                                            stats_frames_lost += 1
                                            del reassembly_buffer[k]
                            except Exception as e: pass

                    except Exception as e: pass 

                elif s is sys.stdin:
                    cmd = sys.stdin.readline().strip()
                    if cmd == "status":
                        print(f"\n--- STATUS {args.node_id} ---")
                        print(f"Vizinhos (RTT):")
                        for k,v in node.neighbors.items(): print(f"  -> {k}: {v['metric']:.1f}ms")
                        print(f"Rotas: {list(node.routing_table.keys())}")
                    elif cmd.startswith("join"):
                        parts = cmd.split()
                        if len(parts) > 1:
                            target = parts[1]
                            if target in node.routing_table:
                                nh = node.routing_table[target].proximo_salto_ip
                                pl = json.dumps({"stream_id": target}).encode('utf-8')
                                pk = node.pack_message(MsgType.STREAM_JOIN, nh, pl)
                                sock.sendto(pk, (nh, DEFAULT_PORT))
                                join_state['active'] = True
                                join_state['stream_id'] = target
                                join_state['target_ip'] = nh
                                join_state['last_sent'] = time.time()
                                join_state['retries'] = 0
                                print(f"[🔌] Pedido JOIN enviado para {nh} (A aguardar ACK...)")
                            else: print("[!] Sem rota. Aguarde flood.")
                    elif cmd.startswith("leave"):
                        parts = cmd.split()
                        if len(parts) > 1:
                            target = parts[1]
                            if target in node.routing_table:
                                nh = node.routing_table[target].proximo_salto_ip
                                pl = json.dumps({"stream_id": target}).encode('utf-8')
                                pk = node.pack_message(MsgType.STREAM_LEAVE, nh, pl)
                                sock.sendto(pk, (nh, DEFAULT_PORT))
                                print(f"[✂️] Pedido LEAVE enviado para {nh}")
                                if "C" in args.node_id: reassembly_buffer.clear()
                                join_state['active'] = False

    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        if video_stream and video_stream.file: video_stream.file.close()
        sock.close()

if __name__ == "__main__":
    main()