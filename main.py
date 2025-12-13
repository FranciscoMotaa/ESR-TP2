import socket
import sys
import select
import json
import argparse
import time
import base64
import math
import os
import subprocess

from utils import get_interface_ip
from overlay_structs import OverlayNode, MsgType, MAX_PACKET_SIZE

DEFAULT_PORT = 50000
BOOTSTRAP_PORT = 6000
MONITOR_PORT = 6001  # Porta UDP do tracker para monitorização
VIDEO_SOURCE = "trailer_the_boys.mp4" 

# --- CONFIGURAÇÃO REDE ---
CHUNK_SIZE = 700  # Tamanho seguro para evitar fragmentação

# --- FUNÇÃO CRÍTICA PARA A ÁRVORE (ADICIONADA) ---
def get_all_ips():
    """Retorna lista de todos os IPs da máquina para o Tracker resolver nomes."""
    try:
        # Comando Linux para listar IPs
        out = subprocess.check_output(['hostname', '-I']).decode('utf-8')
        return out.strip().split()
    except Exception:
        return [get_interface_ip()]
# -------------------------------------------------

# --- CLASSE STREAMER COM ABR (ADAPTIVE BITRATE) ---
class FFmpegStreamer:
    def __init__(self, filename, quality='HIGH'):
        self.filename = filename
        self.current_quality = quality
        print(f"[STREAMER] A iniciar transcodificação ({quality})...")
        
        # --- PERFIS DE QUALIDADE ---
        if quality == 'HIGH':
            scale = "640:480"; v_bitrate = "250k"; a_bitrate = "128k"
        else: # LOW
            scale = "320:240"; v_bitrate = "100k"; a_bitrate = "64k"

        command = [
            'ffmpeg', '-re', '-stream_loop', '-1', '-i', filename,
            '-vf', f'scale={scale}', '-f', 'mpegts', '-c:v', 'mpeg2video',
            '-b:v', v_bitrate, '-g', '15', '-c:a', 'mp2', '-b:a', a_bitrate,
            '-ar', '44100', '-ac', '2', '-'
        ]
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def read_chunk(self, size):
        if self.process: return self.process.stdout.read(size)
        return None

    def close(self):
        if self.process: self.process.terminate()

# --- CLASSE PLAYER ROBUSTO ---
class FFplayPlayer:
    def __init__(self):
        print("[PLAYER] A iniciar ffplay...")
        command = [
            'ffplay', '-f', 'mpegts', '-err_detect', 'ignore_err',
            '-ec', 'favor_inter', '-fflags', '+genpts+igndts',
            '-sync', 'video', '-af', 'aresample=async=1',
            '-infbuf', '-framedrop', '-window_title', 'Streamer 1',
            '-x', '640', '-y', '480', '-loglevel', 'error', '-'
        ]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=None)

    def write_data(self, data):
        try:
            if self.process:
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except BrokenPipeError:
            self.process = None

def get_neighbors_dynamic(tracker_ip, my_id, my_ip):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3.0) 
        print(f"[*] A conectar ao tracker {tracker_ip}...")
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
        # Retorna lista vazia para não crashar, o notify_new_node resolve depois
        return []

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('node_id')
    parser.add_argument('--tracker', help='IP do Tracker', required=True)
    args = parser.parse_args()

    my_ip = get_interface_ip()
    print(f"[*] Nó {args.node_id} ({my_ip}) ONLINE")

    # Obter vizinhos (Estrito do JSON)
    initial_neighbors = get_neighbors_dynamic(args.tracker, args.node_id, my_ip)
    
    node = OverlayNode(args.node_id, my_ip, DEFAULT_PORT)
    for neighbor_ip in initial_neighbors:
        node.neighbors[neighbor_ip] = {'metric': 50.0, 'last_seen': 0}

    # Socket UDP
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 5 * 1024 * 1024)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 5 * 1024 * 1024)
    except: pass
    sock.bind(('0.0.0.0', DEFAULT_PORT))
    sock.setblocking(0)

    # Componentes
    ffmpeg_source = None
    ffplay_sink = None
    current_video_file = None
    
    if args.node_id == "STREAMER1": current_video_file = "trailer_the_boys.mp4"
    elif args.node_id == "STREAMER2": current_video_file = "gta_vi_trailer.mp4"

    if "STREAMER" in args.node_id:
        if current_video_file and os.path.exists(current_video_file):
            ffmpeg_source = FFmpegStreamer(current_video_file, quality='HIGH')
        else:
            print(f"[ERRO] Vídeo não encontrado: {current_video_file}")
            
    if "C" in args.node_id: ffplay_sink = FFplayPlayer()

    # Estado
    join_state = {'active': False, 'stream_id': None, 'target_ip': None, 'last_sent': 0, 'retries': 0}
    last_hello = 0
    last_flood = 0
    last_report = 0        
    last_monitor_update = 0
    
    HELLO_INTERVAL = 1.0 
    FLOOD_INTERVAL = 10.0 
    JOIN_TIMEOUT = 5.0
    MONITOR_UPDATE_INTERVAL = 2.0

    inputs = [sock, sys.stdin]
    frame_seq = 0
    stats_frames_received = 0
    stats_frames_lost = 0  
    last_seq_received = -1
    
    print("[*] Sistema pronto.")
    
    try:
        while True:
            now = time.time()
            
            # --- 1. JOIN Retry ---
            if join_state['active']:
                if now - join_state['last_sent'] > JOIN_TIMEOUT:
                    if join_state['retries'] < 3: 
                        print(f"[⏳] Retry JOIN para {join_state['target_ip']}")
                        pl = json.dumps({"stream_id": join_state['stream_id']}).encode('utf-8')
                        pk = node.pack_message(MsgType.STREAM_JOIN, join_state['target_ip'], pl)
                        sock.sendto(pk, (join_state['target_ip'], DEFAULT_PORT))
                        join_state['last_sent'] = now; join_state['retries'] += 1
                    else:
                        print(f"[❌] Falha no JOIN.")
                        join_state['active'] = False

            # --- 2. HELLOS ---
            if now - last_hello >= HELLO_INTERVAL:
                for n_ip in node.neighbors:
                    pkt = node.pack_message(MsgType.HELLO, n_ip, b"")
                    node.pending_pings[node.sequence_number] = now
                    sock.sendto(pkt, (n_ip, DEFAULT_PORT))
                last_hello = now

            # --- 3. FLOOD ---
            if "STREAMER" in args.node_id and now - last_flood >= FLOOD_INTERVAL:
                flood_payload = json.dumps({
                    "stream_id": args.node_id, "cost": 0, "origin_seq": int(now)
                }).encode('utf-8')
                for n_ip in node.neighbors:
                    pkt = node.pack_message(MsgType.ROUTE_DISCOVERY, n_ip, flood_payload)
                    sock.sendto(pkt, (n_ip, DEFAULT_PORT))
                last_flood = now

            # --- 3.5. TELEMETRIA (COM IPs PARA A ÁRVORE) ---
            if now - last_monitor_update >= MONITOR_UPDATE_INTERVAL:
                try:
                    routing_data = {}
                    for sid, entry in node.routing_table.items():
                        routing_data[sid] = {
                            'next_hop': entry.proximo_salto_ip,
                            'cost': entry.custo_acumulado,
                            'downstream': list(entry.downstream_ips) # Essencial para a árvore
                        }
                    
                    state_update = json.dumps({
                        'node_id': args.node_id,
                        'ips': get_all_ips(), # <--- AQUI ESTÁ A MAGIA
                        'neighbors': {k: v.get('metric', 0) for k,v in node.neighbors.items()},
                        'routing_table': routing_data,
                        'streams': list(node.routing_table.keys())
                    }).encode('utf-8')
                    
                    sock.sendto(state_update, (args.tracker, MONITOR_PORT))
                    last_monitor_update = now
                except Exception: pass

            # --- 4. QoS REPORTS ---
            if "C" in args.node_id and now - last_report >= 2.0:
                if stats_frames_received > 0:
                    total = stats_frames_received + stats_frames_lost
                    loss_rate = (stats_frames_lost / total * 100.0) if total > 0 else 0.0
                    
                    if join_state['stream_id'] and join_state['target_ip']:
                         pl = json.dumps({"stream_id": join_state['stream_id'], "client_id": args.node_id, "loss_rate": loss_rate}).encode('utf-8')
                         pkt = node.pack_message(MsgType.STREAM_REPORT, join_state['target_ip'], pl)
                         sock.sendto(pkt, (join_state['target_ip'], DEFAULT_PORT))
                         stats_frames_received = 0; stats_frames_lost = 0
                last_report = now

            # --- 5. STREAMING ---
            if ffmpeg_source:
                raw_chunk = ffmpeg_source.read_chunk(CHUNK_SIZE)
                if raw_chunk:
                    frame_seq += 1
                    b64 = base64.b64encode(raw_chunk).decode('utf-8')
                    if args.node_id in node.routing_table:
                        entry = node.routing_table[args.node_id]
                        if entry.downstream_ips:
                            pl = json.dumps({"id": args.node_id, "seq": frame_seq, "data": b64}).encode('utf-8')
                            for c in entry.downstream_ips:
                                pkt = node.pack_message(MsgType.STREAM_DATA, c, pl)
                                sock.sendto(pkt, (c, DEFAULT_PORT))
                            time.sleep(0.001)
                elif raw_chunk == b'': ffmpeg_source.close(); ffmpeg_source = None

            # --- EVENT LOOP ---
            readable, _, _ = select.select(inputs, [], [], 0.005)
            for s in readable:
                if s is sock:
                    packet_count = 0
                    while True:
                        try:
                            data, addr = sock.recvfrom(MAX_PACKET_SIZE)
                            packet_count += 1
                        except: break
                        
                        sender_ip_real = addr[0]
                        
                        # Notificações Tracker (Ignoradas em modo estrito se não for vizinho)
                        try:
                            note = json.loads(data.decode('utf-8'))
                            if note.get('type') == 'neighbor_update':
                                # O Tracker estrito já filtrou, então podemos confiar
                                new_ip = note.get('new_neighbor')
                                if new_ip and new_ip not in node.neighbors:
                                    node.neighbors[new_ip] = {'metric': 50.0, 'last_seen': time.time()}
                                    pkt = node.pack_message(MsgType.HELLO, new_ip, b"")
                                    sock.sendto(pkt, (new_ip, DEFAULT_PORT))
                                continue
                        except: pass
                        
                        header, payload = node.unpack_message(data)
                        if not header: continue

                        if header['type'] == MsgType.HELLO:
                            sock.sendto(node.pack_message(MsgType.HELLO_RESPONSE, sender_ip_real, node.handle_hello(header, sender_ip_real)), (sender_ip_real, DEFAULT_PORT))
                        elif header['type'] == MsgType.HELLO_RESPONSE: node.handle_hello_response(payload, sender_ip_real)
                        elif header['type'] == MsgType.ROUTE_DISCOVERY:
                            new_pl = node.handle_flood(header, payload, sender_ip_real)
                            if new_pl: 
                                for n in node.neighbors: 
                                    if n!=sender_ip_real: sock.sendto(node.pack_message(MsgType.ROUTE_DISCOVERY, n, new_pl), (n, DEFAULT_PORT))
                        elif header['type'] == MsgType.STREAM_JOIN:
                            up, ack = node.handle_join(payload, sender_ip_real)
                            if ack: sock.sendto(node.pack_message(MsgType.ACK_JOIN, sender_ip_real, json.dumps({"stream_id": json.loads(payload)['stream_id']}).encode('utf-8')), (sender_ip_real, DEFAULT_PORT))
                            if up and up!="SOURCE":
                                is_srv = False
                                try:
                                    sid = json.loads(payload)['stream_id']
                                    if sid in node.routing_table and len(node.routing_table[sid].downstream_ips)>1: is_srv=True
                                except: pass
                                if not is_srv: sock.sendto(node.pack_message(MsgType.STREAM_JOIN, up, payload), (up, DEFAULT_PORT))
                        elif header['type'] == MsgType.ACK_JOIN:
                            if join_state['active']: join_state['active'] = False; print(f"[✅] Ligação OK.")
                        elif header['type'] == MsgType.STREAM_LEAVE:
                            up = node.handle_leave(payload, sender_ip_real)
                            if up and up!="SOURCE": sock.sendto(node.pack_message(MsgType.STREAM_LEAVE, up, payload), (up, DEFAULT_PORT))
                        elif header['type'] == MsgType.STREAM_REPORT:
                            up = node.handle_report(payload, sender_ip_real)
                            if up and up!="SOURCE": sock.sendto(node.pack_message(MsgType.STREAM_REPORT, up, payload), (up, DEFAULT_PORT))
                            elif up == "SOURCE" and ffmpeg_source:
                                try:
                                    l = json.loads(payload).get('loss_rate', 0.0)
                                    if l > 12.0 and ffmpeg_source.current_quality == 'HIGH':
                                        ffmpeg_source.close(); ffmpeg_source = FFmpegStreamer(current_video_file, 'LOW')
                                    elif l < 2.0 and ffmpeg_source.current_quality == 'LOW':
                                        ffmpeg_source.close(); ffmpeg_source = FFmpegStreamer(current_video_file, 'HIGH')
                                except: pass
                        elif header['type'] == MsgType.STREAM_DATA:
                            try:
                                info = json.loads(payload)
                                sid, seq = info.get('id'), info.get('seq')
                                if sid in node.routing_table:
                                    for c in node.routing_table[sid].downstream_ips: sock.sendto(node.pack_message(MsgType.STREAM_DATA, c, payload), (c, DEFAULT_PORT))
                                if ffplay_sink:
                                    if last_seq_received != -1:
                                        d = seq - last_seq_received
                                        if d > 1 and d < 1000: stats_frames_lost += (d - 1)
                                    last_seq_received = seq
                                    ffplay_sink.write_data(base64.b64decode(info.get('data')))
                                    stats_frames_received += 1
                            except: pass
                        if packet_count > 100: break

                elif s is sys.stdin:
                    cmd = sys.stdin.readline().strip()
                    if cmd == "status":
                        print(f"--- {args.node_id} ---")
                        for k,v in node.neighbors.items(): print(f"-> {k}: {v.get('metric',0):.1f}ms")
                    elif cmd.startswith("join"):
                        parts = cmd.split()
                        if len(parts) > 1:
                            target = parts[1]
                            if target in node.routing_table:
                                nh = node.routing_table[target].proximo_salto_ip
                                pl = json.dumps({"stream_id": target}).encode('utf-8')
                                sock.sendto(node.pack_message(MsgType.STREAM_JOIN, nh, pl), (nh, DEFAULT_PORT))
                                join_state={'active':True, 'stream_id':target, 'target_ip':nh, 'last_sent':time.time(), 'retries':0}
                                print(f"[🔌] Joining {nh}...")
                            else: print("[!] Sem rota.")

    except KeyboardInterrupt: print("\nBye.")
    finally:
        if ffmpeg_source: ffmpeg_source.close()
        sock.close()

if __name__ == "__main__": main()