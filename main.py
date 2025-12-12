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

# --- CLASSE STREAMER COM ABR (ADAPTIVE BITRATE) ---
class FFmpegStreamer:
    def __init__(self, filename, quality='HIGH'):
        self.filename = filename
        self.current_quality = quality
        print(f"[STREAMER] A iniciar transcodificação ({quality})...")
        
        # --- PERFIS DE QUALIDADE ---
        if quality == 'HIGH':
            # Perfil Original: 640x480, 250k video, 128k audio
            scale = "640:480"
            v_bitrate = "250k"
            a_bitrate = "128k"
        else: # LOW
            # Perfil de Resgate: 320x240, 100k video, 64k audio (Poupa banda)
            scale = "320:240"
            v_bitrate = "100k"
            a_bitrate = "64k"

        command = [
            'ffmpeg',
            '-re',
            '-stream_loop', '-1',
            '-i', filename,
            '-vf', f'scale={scale}', # Redimensiona dinamicamente
            '-f', 'mpegts',
            '-c:v', 'mpeg2video',
            '-b:v', v_bitrate,
            '-g', '15',              # Recuperação rápida de imagem
            '-c:a', 'mp2',
            '-b:a', a_bitrate,
            '-ar', '44100',
            '-ac', '2',
            '-'
        ]
        # stderr=subprocess.DEVNULL para manter o terminal limpo
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def read_chunk(self, size):
        if self.process:
            return self.process.stdout.read(size)
        return None

    def close(self):
        if self.process:
            self.process.terminate()

# --- CLASSE PLAYER ROBUSTO ---
class FFplayPlayer:
    def __init__(self):
        print("[PLAYER] A iniciar ffplay...")
        command = [
            'ffplay',
            '-f', 'mpegts',
            
            # --- Tolerância a Falhas ---
            '-err_detect', 'ignore_err',
            '-ec', 'favor_inter',
            '-fflags', '+genpts+igndts',
            
            # --- Sincronização & Áudio ---
            '-sync', 'video',           # Vídeo é Mestre (não trava por causa do som)
            '-af', 'aresample=async=1', # Corrige "underrun" do ALSA
            
            # --- Performance ---
            '-infbuf',                  # Buffer infinito para suavidade
            '-framedrop',               
            
            '-window_title', 'Streamer 1',
            '-x', '640', '-y', '480',
            '-loglevel', 'error',       # Esconde avisos chatos do ALSA
            '-'
        ]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=None)

    def write_data(self, data):
        try:
            if self.process:
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except BrokenPipeError:
            print("[PLAYER] Janela fechada.")
            self.process = None

def get_neighbors_dynamic(tracker_ip, my_id, my_ip):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3.0) 
        print(f"[*] Tentando conectar ao tracker {tracker_ip}:{BOOTSTRAP_PORT} para registar {my_id} ({my_ip})")
        sock.connect((tracker_ip, BOOTSTRAP_PORT))
        request = json.dumps({"id": my_id, "ip": my_ip})
        sock.send(request.encode('utf-8'))
        data = sock.recv(4096)
        try:
            raw = data.decode('utf-8')
        except:
            raw = str(data)
        print(f"[*] Resposta bruta do tracker: {raw}")
        response = json.loads(raw)
        sock.close()

        if response.get("status") == "OK":
            neighbors = response.get("neighbors", [])
            print(f"[*] Tracker: Vizinhos atribuídos -> {neighbors}")
            return neighbors
        print("[WARN] Tracker retornou status != OK")
        return []
    except Exception as e:
        print(f"[ERRO] Falha ao contactar tracker {tracker_ip}:{BOOTSTRAP_PORT}: {e}")
        # Não terminar o processo; devolver lista vazia para permitir funcionamento offline
        return []

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('node_id')
    parser.add_argument('--tracker', help='IP do Tracker', required=True)
    args = parser.parse_args()

    # --- SETUP INICIAL ---
    my_ip = get_interface_ip()
    print(f"[*] Nó {args.node_id} ({my_ip}) ONLINE")

    # Obter vizinhos
    initial_neighbors = get_neighbors_dynamic(args.tracker, args.node_id, my_ip)
    
    node = OverlayNode(args.node_id, my_ip, DEFAULT_PORT)
    for neighbor_ip in initial_neighbors:
        node.neighbors[neighbor_ip] = {'metric': 50.0, 'last_seen': 0}

    # Configurar Socket UDP (Buffer Gigante)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 5 * 1024 * 1024)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 5 * 1024 * 1024)
    except: pass
    sock.bind(('0.0.0.0', DEFAULT_PORT))
    sock.setblocking(0)

    # --- INICIALIZAÇÃO DOS COMPONENTES ---
    # --- INICIALIZAÇÃO DOS COMPONENTES ---
    ffmpeg_source = None
    ffplay_sink = None

    # Lógica de Seleção de Vídeo
    current_video_file = None
    
    if args.node_id == "STREAMER1":
        current_video_file = "trailer_the_boys.mp4"
    elif args.node_id == "STREAMER2":
        current_video_file = "gta_vi_trailer.mp4"

    # Se for um STREAMER (qualquer um deles), prepara a fonte
    if "STREAMER" in args.node_id:
        if current_video_file and os.path.exists(current_video_file):
            # Inicia o streamer com o ficheiro correto para este nó
            ffmpeg_source = FFmpegStreamer(current_video_file, quality='HIGH')
        else:
            print(f"[ERRO CRÍTICO] Sou o {args.node_id} mas não encontro o vídeo: {current_video_file}")
            # Não faz exit, mas avisa que vai ficar parado
    if "C" in args.node_id:
        ffplay_sink = FFplayPlayer()

    # Variáveis de Estado
    join_state = {'active': False, 'stream_id': None, 'target_ip': None, 'last_sent': 0, 'retries': 0}
    
    last_hello = 0
    last_flood = 0
    last_report = 0        # <--- FALTAVA ISTO NO TEU SNIPPET
    last_monitor_update = 0  # Para enviar updates ao tracker
    
    HELLO_INTERVAL = 1.0 
    FLOOD_INTERVAL = 10.0 
    JOIN_TIMEOUT = 5.0
    MONITOR_UPDATE_INTERVAL = 2.0  # Enviar estado ao tracker a cada 2s

    inputs = [sock, sys.stdin]
    frame_seq = 0
    stats_frames_received = 0
    stats_frames_lost = 0  
    last_seq_received = -1
    print("[*] Sistema pronto. Comandos: 'join <STREAM_ID>', 'leave <STREAM_ID>', 'status'.")
    
    try:
        while True:
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

            # --- 3. Flood (Routing) ---
            if "STREAMER" in args.node_id and now - last_flood >= FLOOD_INTERVAL:
                flood_payload = json.dumps({
                    "stream_id": args.node_id,
                    "cost": 0,
                    "origin_seq": int(now)
                }).encode('utf-8')
                for n_ip in node.neighbors:
                    pkt = node.pack_message(MsgType.ROUTE_DISCOVERY, n_ip, flood_payload)
                    sock.sendto(pkt, (n_ip, DEFAULT_PORT))
                last_flood = now

            # --- 3.5. Enviar update de estado ao tracker ---
            if now - last_monitor_update >= MONITOR_UPDATE_INTERVAL:
                try:
                    # Preparar dados de estado
                    routing_table_data = {}
                    for stream_id, route_entry in node.routing_table.items():
                        routing_table_data[stream_id] = {
                            'next_hop': route_entry.proximo_salto_ip,
                            'cost': route_entry.custo_acumulado,
                            'downstream': list(route_entry.downstream_ips)
                        }
                    
                    neighbors_data = {}
                    for n_ip, n_info in node.neighbors.items():
                        neighbors_data[n_ip] = {
                            'metric': n_info.get('metric', 0),
                            'last_seen': n_info.get('last_seen', 0)
                        }
                    
                    # Streams ativos (ou participando)
                    active_streams = list(node.routing_table.keys())
                    
                    state_update = json.dumps({
                        'node_id': args.node_id,
                        'neighbors': neighbors_data,
                        'routing_table': routing_table_data,
                        'streams': active_streams
                    }).encode('utf-8')
                    
                    sock.sendto(state_update, (args.tracker, MONITOR_PORT))
                    last_monitor_update = now
                except Exception as e:
                    pass  # Silenciosamente ignorar erros de monitorização

            # --- 4. Envio de Relatórios QoS (CLIENTE) ---
            if "C" in args.node_id and now - last_report >= 2.0:
                if stats_frames_received > 0:
                    total = stats_frames_received + stats_frames_lost
                    loss_rate = (stats_frames_lost / total * 100.0) if total > 0 else 0.0
                    
                    # DICA DE TESTE: Para testar a troca de qualidade, podes descomentar:
                    # loss_rate = 15.0 

                    if join_state['stream_id'] and join_state['target_ip']:
                         report_payload = json.dumps({
                             "stream_id": join_state['stream_id'], 
                             "client_id": args.node_id, 
                             "loss_rate": loss_rate
                         }).encode('utf-8')
                         pkt = node.pack_message(MsgType.STREAM_REPORT, join_state['target_ip'], report_payload)
                         sock.sendto(pkt, (join_state['target_ip'], DEFAULT_PORT))
                         
                         stats_frames_received = 0
                         stats_frames_lost = 0
                last_report = now

            # --- 5. ENVIO DE VÍDEO (STREAMER) ---
            if ffmpeg_source:
                raw_chunk = ffmpeg_source.read_chunk(CHUNK_SIZE)
                if raw_chunk and len(raw_chunk) > 0:
                    frame_seq += 1
                    b64_data = base64.b64encode(raw_chunk).decode('utf-8')
                    
                    if args.node_id in node.routing_table:
                        clients = node.routing_table[args.node_id].downstream_ips
                        if clients:
                            payload = json.dumps({
                                "id": args.node_id, 
                                "seq": frame_seq, 
                                "data": b64_data
                            }).encode('utf-8')

                            for client_ip in clients:
                                pkt = node.pack_message(MsgType.STREAM_DATA, client_ip, payload)
                                sock.sendto(pkt, (client_ip, DEFAULT_PORT))
                            
                            time.sleep(0.001) # Pacing rápido
                elif raw_chunk == b'':
                    print("[FIM] Vídeo terminou.")
                    ffmpeg_source.close()
                    ffmpeg_source = None

            # --- EVENT LOOP ---
            readable, _, _ = select.select(inputs, [], [], 0.005)
            
            for s in readable:
                if s is sock:
                    packet_count = 0
                    while True:
                        try:
                            data, addr = sock.recvfrom(MAX_PACKET_SIZE)
                            packet_count += 1
                        except BlockingIOError: break
                        except Exception: break
                        
                        sender_ip_real = addr[0]
                        
                        # Verificar se é uma notificação do bootstrapper (JSON puro, sem header overlay)
                        try:
                            notification = json.loads(data.decode('utf-8'))
                            if notification.get('type') == 'neighbor_update':
                                new_neighbor_ip = notification.get('new_neighbor')
                                new_neighbor_id = notification.get('neighbor_id')
                                if new_neighbor_ip and new_neighbor_ip not in node.neighbors:
                                    # Adicionar novo vizinho dinamicamente
                                    node.neighbors[new_neighbor_ip] = {
                                        'metric': 50.0,  # Métrica inicial
                                        'last_seen': time.time()
                                    }
                                    print(f"\n NOVO VIZINHO: {new_neighbor_id} ({new_neighbor_ip}) adicionado dinamicamente!")
                                    
                                    # Enviar HELLO imediatamente para estabelecer conexão
                                    pkt = node.pack_message(MsgType.HELLO, new_neighbor_ip, b"")
                                    sock.sendto(pkt, (new_neighbor_ip, DEFAULT_PORT))
                                continue
                        except: pass
                        
                        header, payload = node.unpack_message(data)
                        if not header: continue

                        # --- PROCESSAMENTO ---
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
                            upstream_ip, send_ack = node.handle_join(payload, sender_ip_real)
                            if send_ack:
                                try:
                                    jdata = json.loads(payload.decode('utf-8'))
                                    sid = jdata['stream_id']
                                    ack_pl = json.dumps({"stream_id": sid}).encode('utf-8')
                                    ack_pkt = node.pack_message(MsgType.ACK_JOIN, sender_ip_real, ack_pl)
                                    sock.sendto(ack_pkt, (sender_ip_real, DEFAULT_PORT))
                                except: pass

                            if upstream_ip and upstream_ip != "SOURCE":
                                # Verifica se já serve para evitar loops de JOIN
                                is_serving = False
                                try:
                                    jdata = json.loads(payload.decode('utf-8'))
                                    sid = jdata['stream_id']
                                    if sid in node.routing_table and len(node.routing_table[sid].downstream_ips) > 1:
                                        is_serving = True
                                except: pass
                                if not is_serving:
                                    pkt = node.pack_message(MsgType.STREAM_JOIN, upstream_ip, payload)
                                    sock.sendto(pkt, (upstream_ip, DEFAULT_PORT))

                        elif header['type'] == MsgType.ACK_JOIN:
                            if join_state['active']:
                                print(f"[✅] ACK recebido! Ligação OK.")
                                join_state['active'] = False 

                        elif header['type'] == MsgType.STREAM_LEAVE:
                            upstream_prune = node.handle_leave(payload, sender_ip_real)
                            if upstream_prune and upstream_prune != "SOURCE":
                                pkt = node.pack_message(MsgType.STREAM_LEAVE, upstream_prune, payload)
                                sock.sendto(pkt, (upstream_prune, DEFAULT_PORT))

                        # --- ADAPTIVE BITRATE LOGIC ---
                        elif header['type'] == MsgType.STREAM_REPORT:
                            # 1. Router: Encaminhar
                            upstream_report = node.handle_report(payload, sender_ip_real)
                            if upstream_report and upstream_report != "SOURCE":
                                pkt = node.pack_message(MsgType.STREAM_REPORT, upstream_report, payload)
                                sock.sendto(pkt, (upstream_report, DEFAULT_PORT))
                            
                            # 2. Streamer: Decidir Qualidade
                            elif upstream_report == "SOURCE" and ffmpeg_source:
                                try:
                                    info = json.loads(payload.decode('utf-8'))
                                    loss = info.get('loss_rate', 0.0)
                                    
                                    if loss > 12.0 and ffmpeg_source.current_quality == 'HIGH':
                                        print(f"\n[⚠️] CONGESTIONAMENTO (Perda: {loss:.1f}%) -> LOW PROFILE")
                                        ffmpeg_source.close()
                                        ffmpeg_source = FFmpegStreamer(VIDEO_SOURCE, quality='LOW')
                                    
                                    elif loss < 2.0 and ffmpeg_source.current_quality == 'LOW':
                                        print(f"\nREDE RECUPERADA (Perda: {loss:.1f}%) -> HIGH PROFILE")
                                        ffmpeg_source.close()
                                        ffmpeg_source = FFmpegStreamer(VIDEO_SOURCE, quality='HIGH')
                                except Exception as e: print(f"Erro ABR: {e}")

                        # --- VÍDEO DATA ---
                        elif header['type'] == MsgType.STREAM_DATA:
                            try:
                                info = json.loads(payload.decode('utf-8'))
                                s_id = info.get('id')
                                seq = info.get('seq') # Pega o número de sequência
                                
                                # 1. Router: Forwarding
                                if s_id in node.routing_table:
                                    for child in node.routing_table[s_id].downstream_ips:
                                        if child != sender_ip_real:
                                            pkt = node.pack_message(MsgType.STREAM_DATA, child, payload)
                                            sock.sendto(pkt, (child, DEFAULT_PORT))
                                
                                # 2. Cliente: Play & Stats
                                if ffplay_sink:
                                    # --- LÓGICA DE DETEÇÃO DE PERDA (NOVO) ---
                                    if last_seq_received != -1:
                                        # Se recebi o 10 e agora veio o 15, perdi 4 pacotes (11,12,13,14)
                                        diff = seq - last_seq_received
                                        if diff > 1:
                                            # Proteção para não contar negativos se os pacotes vierem trocados (jitter)
                                            # ou se o stream reiniciar (reset do seq)
                                            if diff < 1000: 
                                                stats_frames_lost += (diff - 1)
                                            else:
                                                # Assumimos que foi um reset do stream
                                                pass
                                    last_seq_received = seq
                                    # -----------------------------------------

                                    b64_data = info.get('data')
                                    raw_data = base64.b64decode(b64_data)
                                    ffplay_sink.write_data(raw_data)
                                    stats_frames_received += 1
                                    
                                    if stats_frames_received % 100 == 0:
                                        print(f"\r[FF] Packets RX: {stats_frames_received} | Lost: {stats_frames_lost}", end="")

                            except Exception as e: pass
                        if packet_count > 100: break

                elif s is sys.stdin:
                    cmd = sys.stdin.readline().strip()
                    if cmd == "status":
                        print(f"\n--- STATUS {args.node_id} ---")
                        for k,v in node.neighbors.items(): print(f"  -> {k}: {v['metric']:.1f}ms")
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
                                print(f"[🔌] Pedido JOIN enviado para {nh}")
                            else: print("[!] Sem rota. Aguarde flood.")

    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        if ffmpeg_source: ffmpeg_source.close()
        sock.close()

if __name__ == "__main__":
    main()