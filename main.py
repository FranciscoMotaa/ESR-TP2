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
MONITOR_PORT = 6001  
CHUNK_SIZE = 1400 

def get_all_ips():
    """Retorna lista de todos os IPs da máquina."""
    try:
        out = subprocess.check_output(['hostname', '-I']).decode('utf-8')
        return out.strip().split()
    except:
        return [get_interface_ip()]

class FFmpegStreamer:
    def __init__(self, filename, quality='LOW'): # <--- FORÇAR 'LOW' COMO DEFAULT
        self.filename = filename
        self.current_quality = quality
        print(f"[STREAMER] A iniciar transcodificação ({quality})...")
        
        # AJUSTE BRUTAL: Reduzir resolução e bitrate para garantir Áudio
        if quality == 'HIGH':
            scale = "640:480"; v_bitrate = "600k" # Reduzi de 800k para 600k
        else: 
            scale = "320:240"; v_bitrate = "150k" # Reduzi de 200k para 150k (Ultra leve)

        command = [
            'ffmpeg', '-re', '-stream_loop', '-1', '-i', filename,
            '-vf', f'scale={scale}', 
            '-f', 'mpegts', 
            # Codec de Vídeo Leve
            '-c:v', 'mpeg2video', '-b:v', v_bitrate, '-maxrate', v_bitrate, 
            '-bufsize', v_bitrate,
            '-g', '24', # Keyframe a cada 24 frames (menos dados)
            # Codec de Áudio (AAC é mais robusto que AC3 em bitrates baixos)
            '-c:a', 'aac', '-b:a', '64k', '-ac', '1', 
            '-ar', '44100', 
            '-'
        ]
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def read_chunk(self, size):
        if self.process: return self.process.stdout.read(size)
        return None

    def close(self):
        if self.process: self.process.terminate()
class FFplayPlayer:
    def __init__(self):
        print("[PLAYER] A iniciar ffplay (Modo Baixa Latência)...")
        command = [
            'ffplay', '-f', 'mpegts',
            '-fflags', 'nobuffer',
            '-flags', 'low_delay',
            '-probesize', '32',
            '-analyzeduration', '0',
            '-err_detect', 'ignore_err', '-ec', 'favor_inter',
            '-sync', 'ext',
            '-framedrop', '-window_title', 'Streamer', '-x', '640', '-y', '480',
            '-'
        ]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def write_data(self, data):
        if not self.process: return
        try:
            self.process.stdin.write(data)
            self.process.stdin.flush()
        except (BrokenPipeError, IOError):
            print("[PLAYER] Aviso: Pipe quebrado (Janela fechada?).")
            self.process = None

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
        return response.get("neighbors", []) if response.get("status") == "OK" else []
    except: return []

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('node_id')
    parser.add_argument('--tracker', help='IP do Tracker', required=True)
    args = parser.parse_args()

    my_ip = get_interface_ip()
    print(f"[*] Nó {args.node_id} ({my_ip}) ONLINE")

    # 1. Obter vizinhos do Bootstrapper
    static_neighbors_list = get_neighbors_dynamic(args.tracker, args.node_id, my_ip)
    
    node = OverlayNode(args.node_id, my_ip, DEFAULT_PORT)
    
    # 2. CRIAÇÃO DO SOCKET (Isto tem de vir ANTES de tentar enviar qualquer coisa)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # --- CORREÇÃO CRÍTICA: SO_REUSEADDR ---
    # Permite reiniciar o script instantaneamente sem erro "Address already in use"
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 10*1024*1024)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 10*1024*1024)
    except: pass
    
    sock.bind(('0.0.0.0', DEFAULT_PORT))
    sock.setblocking(0)

    # 3. ARRANQUE RÁPIDO (Fast Convergence)
    # Agora que o socket existe, enviamos os pacotes imediatos para "acordar" a rede
    print("[🚀] ARRANQUE RÁPIDO: A forçar entrada na rede...")

    # A) Forçar HELLO aos vizinhos estáticos
    for n_ip in static_neighbors_list:
        if n_ip not in node.neighbors:
            node.neighbors[n_ip] = {'metric': 50.0, 'last_seen': time.time()}
        
        # Envia Hello imediato
        pkt = node.pack_message(MsgType.HELLO, n_ip, b"")
        sock.sendto(pkt, (n_ip, DEFAULT_PORT))

    # B) Se for STREAMER, forçar FLOOD imediato
    if "STREAMER" in args.node_id:
        print("[🌊] STREAMER: A inundar rede para anunciar rotas...")
        flood_payload = json.dumps({"stream_id": args.node_id, "cost": 0, "origin_seq": int(time.time())}).encode('utf-8')
        for n_ip in node.neighbors:
            pkt = node.pack_message(MsgType.ROUTE_DISCOVERY, n_ip, flood_payload)
            sock.sendto(pkt, (n_ip, DEFAULT_PORT))

    # 4. Configuração de Multimédia
    ffmpeg_source = None
    ffplay_sink = None
    current_video_file = None
    
    if args.node_id == "STREAMER1": current_video_file = "trailer_the_boys.mp4"
    elif args.node_id == "STREAMER2": current_video_file = "gta_vi_trailer.mp4"

    if "STREAMER" in args.node_id:
        if current_video_file and os.path.exists(current_video_file):
            print(f"[STREAMER] Ficheiro pronto. Aguardando clientes...")
        else:
            print(f"[ERRO] Vídeo não encontrado: {current_video_file}")
    
    if "C" in args.node_id: ffplay_sink = FFplayPlayer()

    # 5. Inicialização de Estado
    join_state = {'active': False, 'stream_id': None, 'target_ip': None, 'last_sent': 0, 'retries': 0}
    
    # Timers iniciados com 0 garantem execução, mas o Arranque Rápido acima já tratou da prioridade
    last_hello = time.time() 
    last_flood = time.time()
    last_report = 0
    last_monitor_update = 0
    last_upstream_maint = 0
    last_check_dead = 0
    last_downstream_clean = 0
    
    last_quality_switch = 0 
    node.downstream_timers = {} 
    
    HELLO_INTERVAL = 1.0   
    FLOOD_INTERVAL = 1.0   
    JOIN_TIMEOUT = 2.5     
    
    inputs = [sock, sys.stdin]
    frame_seq = 0
    stats_frames_received = 0
    stats_frames_lost = 0  
    last_seq_received = -1
    interval_start_seq = -1

    print("[*] Sistema pronto. (Digite 'join STREAMER1' para ver)")
    
    # 6. Loop Principal
    try:
        while True:
            now = time.time()
            
            # --- 0. DETECTOR DE MORTOS ---
            if now - last_check_dead >= 1.0:
                dead_nodes = []
                for nid, stats in node.neighbors.items():
                    if now - stats.get('last_seen', 0) > 15.0: dead_nodes.append(nid)
                for dead_ip in dead_nodes:
                    print(f"[☠️] VIZINHO MORREU: {dead_ip}. Limpando rotas...")
                    del node.neighbors[dead_ip]
                    to_remove = [s for s, r in node.routing_table.items() if r.proximo_salto_ip == dead_ip]
                    for s in to_remove: del node.routing_table[s]
                    for r in node.routing_table.values():
                        if dead_ip in r.downstream_ips: r.downstream_ips.remove(dead_ip)
                last_check_dead = now

            # --- 0.5 LIMPEZA DE CLIENTES INATIVOS ---
            if now - last_downstream_clean >= 1.0:
                for sid in list(node.routing_table.keys()):
                    entry = node.routing_table[sid]
                    dead_children = []
                    for child in entry.downstream_ips:
                        last_seen = node.downstream_timers.get((sid, child), 0)
                        if now - last_seen > 12.0:
                            dead_children.append(child)
                    for child in dead_children:
                        print(f"[♻️] Cliente expirou: {child} saiu da stream {sid}")
                        entry.downstream_ips.remove(child)
                        if (sid, child) in node.downstream_timers: del node.downstream_timers[(sid, child)]
                last_downstream_clean = now

            # --- 1. Join Retries ---
            if join_state['active'] and now - join_state['last_sent'] > JOIN_TIMEOUT:
                if join_state['retries'] < 5: 
                    if join_state['stream_id'] in node.routing_table:
                        best_hop = node.routing_table[join_state['stream_id']].proximo_salto_ip
                        if best_hop != join_state['target_ip'] and best_hop != "SELF":
                            print(f"[🔄] Rota mudou durante o Join: {join_state['target_ip']} -> {best_hop}")
                            join_state['target_ip'] = best_hop

                    print(f"[DEBUG] Retentando JOIN para {join_state['target_ip']}...")
                    pl = json.dumps({"stream_id": join_state['stream_id']}).encode('utf-8')
                    pk = node.pack_message(MsgType.STREAM_JOIN, join_state['target_ip'], pl)
                    sock.sendto(pk, (join_state['target_ip'], DEFAULT_PORT))
                    join_state['last_sent'] = now; join_state['retries'] += 1
                else: 
                    print("[!] Falha ao conectar: Timeout.")
                    join_state['active'] = False

            # --- 2. Hellos & Health Check ---
            if now - last_hello >= HELLO_INTERVAL:
                for n_ip in node.neighbors:
                    pkt = node.pack_message(MsgType.HELLO, n_ip, b"")
                    node.pending_pings[node.sequence_number] = now
                    sock.sendto(pkt, (n_ip, DEFAULT_PORT))
                last_hello = now
                
                switches = node.check_active_routes_health()
                for (sid, old_p, new_p, new_cost) in switches:
                    pl_l = json.dumps({"stream_id": sid}).encode('utf-8')
                    sock.sendto(node.pack_message(MsgType.STREAM_LEAVE, old_p, pl_l), (old_p, DEFAULT_PORT))
                    pl_j = json.dumps({"stream_id": sid}).encode('utf-8')
                    sock.sendto(node.pack_message(MsgType.STREAM_JOIN, new_p, pl_j), (new_p, DEFAULT_PORT))
                    
                    node.routing_table[sid].proximo_salto_ip = new_p
                    node.routing_table[sid].custo_acumulado = new_cost
                    if new_p in node.routing_table[sid].downstream_ips:
                        node.routing_table[sid].downstream_ips.remove(new_p)
                    print(f"[⚡] Rota alterada: {old_p} -> {new_p}")

            # --- 3. Flood ---
            if "STREAMER" in args.node_id and now - last_flood >= FLOOD_INTERVAL:
                flood_payload = json.dumps({"stream_id": args.node_id, "cost": 0, "origin_seq": int(now)}).encode('utf-8')
                for n_ip in node.neighbors:
                    pkt = node.pack_message(MsgType.ROUTE_DISCOVERY, n_ip, flood_payload)
                    sock.sendto(pkt, (n_ip, DEFAULT_PORT))
                last_flood = now

            # --- 4. Monitorização ---
            if now - last_monitor_update >= 2.0:
                try:
                    r_data = {}
                    for sid, entry in node.routing_table.items():
                        r_data[sid] = {'next_hop': entry.proximo_salto_ip, 'cost': entry.custo_acumulado, 'downstream': list(entry.downstream_ips)}
                    update = json.dumps({
                        'node_id': args.node_id, 'ips': get_all_ips(),
                        'neighbors': {k: v.get('metric',0) for k,v in node.neighbors.items()},
                        'routing_table': r_data, 'streams': list(node.routing_table.keys())
                    }).encode('utf-8')
                    sock.sendto(update, (args.tracker, MONITOR_PORT))
                    last_monitor_update = now
                except: pass

            # --- 5. Manutenção Rota ---
            if now - last_upstream_maint >= 2.0:
                for sid, entry in node.routing_table.items():
                    if args.node_id != sid and entry.proximo_salto_ip != "SELF":
                        am_i_consuming = (ffplay_sink is not None and join_state.get('stream_id') == sid)
                        if entry.downstream_ips or am_i_consuming: 
                            pl = json.dumps({"stream_id": sid}).encode('utf-8')
                            pkt = node.pack_message(MsgType.STREAM_JOIN, entry.proximo_salto_ip, pl)
                            sock.sendto(pkt, (entry.proximo_salto_ip, DEFAULT_PORT))
                last_upstream_maint = now

            # --- 6. QoS ---
            if "C" in args.node_id and now - last_report >= 2.0:
                if stats_frames_received > 0:
                    expect = last_seq_received - interval_start_seq + 1
                    lost = expect - stats_frames_received
                    if lost < 0: lost = 0
                    rate = (lost / expect * 100.0) if expect > 0 else 0.0
                    if join_state['stream_id']:
                         pl = json.dumps({"stream_id": join_state['stream_id'], "client_id": args.node_id, "loss_rate": rate}).encode('utf-8')
                         sock.sendto(node.pack_message(MsgType.STREAM_REPORT, join_state['target_ip'], pl), (join_state['target_ip'], DEFAULT_PORT))
                         stats_frames_received = 0; interval_start_seq = -1; last_seq_received = -1
                last_report = now

            # --- 7. Streamer Send ---
            if "STREAMER" in args.node_id:
                has_clients = False
                if args.node_id in node.routing_table:
                     if node.routing_table[args.node_id].downstream_ips: has_clients = True
                
                if has_clients and ffmpeg_source is None:
                     if current_video_file and os.path.exists(current_video_file):
                        print(f"[STREAMER] Cliente detetado! A iniciar streaming...")
                        ffmpeg_source = FFmpegStreamer(current_video_file, quality='HIGH')
                        # AJUSTE 2: Define o momento de início para ativar o período de graça (20s)
                        last_quality_switch = time.time()
                elif not has_clients and ffmpeg_source is not None:
                     print("[STREAMER] Sem clientes. A pausar...")
                     ffmpeg_source.close(); ffmpeg_source = None

            if ffmpeg_source:
                raw = ffmpeg_source.read_chunk(CHUNK_SIZE)
                if raw:
                    frame_seq += 1
                    b64 = base64.b64encode(raw).decode('utf-8')
                    if args.node_id in node.routing_table:
                        entry = node.routing_table[args.node_id]
                        if entry.downstream_ips:
                            pl = json.dumps({"id": args.node_id, "seq": frame_seq, "data": b64}).encode('utf-8')
                            for c in entry.downstream_ips:
                                pkt = node.pack_message(MsgType.STREAM_DATA, c, pl)
                                sock.sendto(pkt, (c, DEFAULT_PORT))
                            time.sleep(0.002)
                elif raw == b'': ffmpeg_source.close(); ffmpeg_source = None

            # --- EVENT LOOP ---
            # --- EVENT LOOP (CORRIGIDO) ---
            readable, _, _ = select.select(inputs, [], [], 0.005)
            for s in readable:
                if s is sock:
                    cnt = 0
                    while True:
                        try: 
                            data, addr = sock.recvfrom(MAX_PACKET_SIZE)
                        except: break # Socket vazio ou erro de leitura
                        
                        cnt += 1; sender_ip = addr[0]
                        
                        # Atualiza tabela de vizinhos se recebermos algo
                        if sender_ip not in node.neighbors:
                            if sender_ip in static_neighbors_list:
                                node.neighbors[sender_ip] = {'metric': 50.0, 'last_seen': time.time()}
                        else: node.neighbors[sender_ip]['last_seen'] = time.time()
                        
                        # 1. Tenta ver se é JSON puro (Bootstrapper/Tracker)
                        try:
                            note = json.loads(data.decode('utf-8'))
                            if note.get('type') == 'neighbor_update': continue 
                        except: pass
                        
                        # 2. Tenta desembrulhar pacote binário (Overlay)
                        # --- PROTEÇÃO CONTRA CRASH AQUI ---
                        try:
                            header, payload = node.unpack_message(data)
                        except Exception:
                            # Se falhar a desembrulhar, ignora o pacote e segue em frente
                            continue
                        # ----------------------------------

                        if not header: continue

                        if header['type'] == MsgType.HELLO:
                            sock.sendto(node.pack_message(MsgType.HELLO_RESPONSE, sender_ip, node.handle_hello(header, sender_ip)), (sender_ip, DEFAULT_PORT))
                        elif header['type'] == MsgType.HELLO_RESPONSE: node.handle_hello_response(payload, sender_ip)
                        elif header['type'] == MsgType.ROUTE_DISCOVERY:
                            new_pl = node.handle_flood(header, payload, sender_ip)
                            if new_pl: 
                                for n in node.neighbors: 
                                    if n!=sender_ip: sock.sendto(node.pack_message(MsgType.ROUTE_DISCOVERY, n, new_pl), (n, DEFAULT_PORT))
                        
                        elif header['type'] == MsgType.STREAM_JOIN:
                            try:
                                sid = json.loads(payload.decode('utf-8')).get('stream_id')
                                up, ack = node.handle_join(payload, sender_ip)
                                node.downstream_timers[(sid, sender_ip)] = time.time()
                                if ack: 
                                    ack_pl = json.dumps({"stream_id": sid}).encode('utf-8')
                                    sock.sendto(node.pack_message(MsgType.ACK_JOIN, sender_ip, ack_pl), (sender_ip, DEFAULT_PORT))
                                if up and up!="SOURCE":
                                    is_srv = False
                                    try:
                                        if sid in node.routing_table and len(node.routing_table[sid].downstream_ips)>1: is_srv=True
                                    except: pass
                                    if not is_srv: 
                                        sock.sendto(node.pack_message(MsgType.STREAM_JOIN, up, payload), (up, DEFAULT_PORT))
                            except: pass

                        elif header['type'] == MsgType.ACK_JOIN:
                            if join_state['active']: join_state['active'] = False; print(f"[✅] Ligação OK!")

                        elif header['type'] == MsgType.STREAM_LEAVE:
                            up = node.handle_leave(payload, sender_ip)
                            if up and up!="SOURCE": sock.sendto(node.pack_message(MsgType.STREAM_LEAVE, up, payload), (up, DEFAULT_PORT))
                        
                        elif header['type'] == MsgType.STREAM_REPORT:
                            up = node.handle_report(payload, sender_ip)
                            if up and up!="SOURCE": sock.sendto(node.pack_message(MsgType.STREAM_REPORT, up, payload), (up, DEFAULT_PORT))
                            elif up == "SOURCE" and ffmpeg_source:
                                try:
                                    l = json.loads(payload).get('loss_rate', 0.0)
                                    # AJUSTE 3: Período de Graça
                                    if now - last_quality_switch > 20.0:
                                        if l > 15.0 and ffmpeg_source.current_quality == 'HIGH':
                                            ffmpeg_source.close(); ffmpeg_source = FFmpegStreamer(current_video_file, 'LOW'); last_quality_switch = now
                                            print(f"[QoS] Perda Alta ({l:.1f}%). A baixar qualidade...")
                                        elif l < 5.0 and ffmpeg_source.current_quality == 'LOW':
                                            ffmpeg_source.close(); ffmpeg_source = FFmpegStreamer(current_video_file, 'HIGH'); last_quality_switch = now
                                            print(f"[QoS] Rede Estável ({l:.1f}%). A subir qualidade...")
                                except: pass
                        
                        elif header['type'] == MsgType.STREAM_DATA:
                            try:
                                info = json.loads(payload); sid, seq = info.get('id'), info.get('seq')
                                if sid in node.routing_table:
                                    for c in node.routing_table[sid].downstream_ips:
                                        if c != sender_ip:
                                            sock.sendto(node.pack_message(MsgType.STREAM_DATA, c, payload), (c, DEFAULT_PORT))
                                if ffplay_sink:
                                    if last_seq_received != -1:
                                        d = seq - last_seq_received
                                        if d > 1 and d < 1000: stats_frames_lost += (d - 1)
                                    last_seq_received = seq
                                    ffplay_sink.write_data(base64.b64decode(info.get('data')))
                                    stats_frames_received += 1
                                    if stats_frames_received % 60 == 0: print(".", end="", flush=True)
                            except: pass
                        if cnt > 100: break

                elif s is sys.stdin:
                    cmd = sys.stdin.readline().strip()
                    if cmd == "status":
                        print(f"--- {args.node_id} ---")
                        for k,v in node.neighbors.items(): print(f"-> {k}: {v.get('metric',0):.1f}ms")
                        for sid, r in node.routing_table.items(): print(f"Rota {sid}: via {r.proximo_salto_ip} custo {r.custo_acumulado:.1f}")
                    elif cmd.startswith("join"):
                        parts = cmd.split()
                        if len(parts) > 1:
                            target = parts[1]
                            if target in node.routing_table:
                                nh = node.routing_table[target].proximo_salto_ip
                                pl = json.dumps({"stream_id": target}).encode('utf-8')
                                sock.sendto(node.pack_message(MsgType.STREAM_JOIN, nh, pl), (nh, DEFAULT_PORT))
                                join_state={'active':True, 'stream_id':target, 'target_ip':nh, 'last_sent':time.time(), 'retries':0}
                                print(f"[🔌] Joining {target} via {nh}...")
                            else: print("[!] Sem rota.")

    except KeyboardInterrupt: print("\nBye.")
    finally:
        if ffmpeg_source: ffmpeg_source.close()
        sock.close()

if __name__ == "__main__": main()