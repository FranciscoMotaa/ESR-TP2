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
import traceback
import threading

from utils import get_interface_ip, measure_rtt
from overlay_structs import OverlayNode, MsgType, MAX_PACKET_SIZE

DEFAULT_PORT = 50000
BOOTSTRAP_PORT = 6000
MONITOR_PORT = 6001  # Porta UDP do tracker para monitorização
VIDEO_SOURCE = "trailer_the_boys.mp4"
RETRY_WAIT_INTERVAL = 5.0

# --- CONFIGURAÇÃO REDE ---
CHUNK_SIZE = 500  # Tamanho seguro para evitar fragmentação

# --- FUNÇÃO CRÍTICA PARA A ÁRVORE ---
def get_all_ips():
    """Retorna lista de todos os IPs da máquina."""
    try:
        out = subprocess.check_output(['hostname', '-I']).decode('utf-8')
        return out.strip().split()
    except:
        return [get_interface_ip()]

class FFmpegStreamer:
    def __init__(self, filename, quality='LOW'): 
        self.filename = filename
        self.current_quality = quality
        print(f"[STREAMER] A iniciar transcodificação ({quality})...")
        
        # AJUSTE: Reduzir resolução e bitrate para garantir Áudio/Vídeo fluido
        if quality == 'HIGH':
            scale = "640:480"; v_bitrate = "600k"
        else: 
            scale = "320:240"; v_bitrate = "150k"

        command = [
            'ffmpeg', '-re', '-stream_loop', '-1', '-i', filename,
            '-vf', f'scale={scale}', 
            '-f', 'mpegts', 
            '-c:v', 'mpeg2video', '-b:v', v_bitrate, '-maxrate', v_bitrate, 
            '-bufsize', v_bitrate,
            '-g', '24', 
            '-c:a', 'aac', '-b:a', '64k', '-ac', '1', 
            '-ar', '44100', 
            '-'
        ]
        # Capturar stderr para diagnosticar erros do ffmpeg
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._stderr_thread = threading.Thread(target=self._stderr_reader, daemon=True)
        self._stderr_thread.start()

    def _stderr_reader(self):
        try:
            for line in iter(self.process.stderr.readline, b''):
                try:
                    # print(f"[FFMPEG-ERR] {line.decode('utf-8', errors='replace').rstrip()}")
                    pass 
                except Exception: pass
        except Exception: pass

    def read_chunk(self, size):
        if not self.process: return None
        if self.process.poll() is not None:
            try:
                remaining = self.process.stdout.read(size)
                return remaining if remaining else b''
            except: return b''
        try:
            return self.process.stdout.read(size)
        except: return None

    def close(self):
        try:
            if self.process: self.process.terminate()
        except: pass
        finally: self.process = None

# --- CLASSE PLAYER ROBUSTO ---
class FFplayPlayer:
    def __init__(self, title: str = None):
        print("[PLAYER] A iniciar ffplay...")
        have_display = bool(os.environ.get('DISPLAY'))
        cmd = ['ffplay', '-f', 'mpegts', '-err_detect', 'ignore_err', '-ec', 'favor_inter',
               '-fflags', '+genpts+igndts', '-sync', 'video', '-af', 'aresample=async=1',
               '-infbuf', '-framedrop', '-loglevel', 'error']
        
        if have_display:
            win_title = title if title else 'Player'
            cmd += ['-window_title', win_title, '-x', '640', '-y', '480']
        else:
            cmd += ['-nodisp']

        cmd += ['-']
        env = os.environ.copy()
        if not have_display: env['SDL_VIDEODRIVER'] = 'dummy'

        self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self._stderr_thread = threading.Thread(target=self._stderr_reader, daemon=True)
        self._stderr_thread.start()

    def _stderr_reader(self):
        try:
            for line in iter(self.process.stderr.readline, b''):
                try:
                    print(f"[FFPLAY-ERR] {line.decode('utf-8', errors='replace').rstrip()}")
                except: pass
        except: pass

    def write_data(self, data):
        if not self.process: return
        try:
            self.process.stdin.write(data)
            self.process.stdin.flush()
        except (BrokenPipeError, IOError):
            print("[PLAYER] Aviso: Pipe quebrado (Janela fechada?).")
            self.process = None

    def close(self):
        if self.process:
            print("[PLAYER] A fechar janela de vídeo...")
            try:
                self.process.terminate()
                self.process.wait(timeout=1) # Espera que feche
            except: 
                pass
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

    ips = get_all_ips()
    my_ip = None
    for ip in ips:
        if not ip.startswith('127.'):
            my_ip = ip
            break
    if not my_ip: my_ip = ips[0] if ips else get_interface_ip()
    print(f"[*] Nó {args.node_id} ({my_ip}) ONLINE")

    # 1. Obter vizinhos do Bootstrapper
    initial_neighbors = get_neighbors_dynamic(args.tracker, args.node_id, my_ip)
    
    node = OverlayNode(args.node_id, my_ip, DEFAULT_PORT)
    
    # ===== CRIPTOGRAFIA AES-256-GCM (ATIVA POR PADRÃO) =====
    encryption_passphrase = os.environ.get('STREAM_ENCRYPTION_KEY', 'default_secure_passphrase_2025')
    enable_encryption = os.environ.get('ENABLE_ENCRYPTION', '1') == '1'
    
    print(f"[SECURITY] Configuração: ENABLE_ENCRYPTION={enable_encryption}, KEY={'personalizada' if 'STREAM_ENCRYPTION_KEY' in os.environ else 'padrão'}")
    
    if enable_encryption:
        try:
            node.security.enable(encryption_passphrase)
            # Verificar se realmente está ativa
            if not node.security.is_enabled():
                print(f"[SECURITY] ❌ ERRO: Criptografia não foi ativada corretamente!")
                sys.exit(1)
            print(f"[SECURITY] ✅ Criptografia AES-256-GCM ATIVADA para {args.node_id}")
            print(f"[SECURITY] 🔒 TODOS os pacotes serão cifrados!")
        except Exception as e:
            print(f"[SECURITY] ❌ ERRO ao ativar criptografia: {e}")
            traceback.print_exc()
            sys.exit(1)
    else:
        print(f"[SECURITY] ⚠️  Criptografia DESATIVADA (modo debug) - PACOTES EM CLARO!")
    
    # Adicionar vizinhos iniciais (medir RTT inicialmente quando possível)
    for neighbor_ip in initial_neighbors:
        try:
            rtt = measure_rtt(neighbor_ip, timeout=1)
        except Exception:
            rtt = None
        node.neighbors[neighbor_ip] = {'metric': rtt, 'last_seen': time.time(), 'state': 'alive', 'missed_hellos': 0}

    # Socket UDP
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 10*1024*1024)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 10*1024*1024)
    except: pass
    
    sock.bind(('0.0.0.0', DEFAULT_PORT))
    sock.setblocking(0)

    # 3. ARRANQUE RÁPIDO
    print("[🚀] ARRANQUE RÁPIDO: A contactar vizinhos...")
    for n_ip in initial_neighbors:
        try:
            # Hello
            pkt = node.pack_message(MsgType.HELLO, n_ip, b"")
            # Registar ping para medir RTT quando chegar HELLO_RESPONSE
            try:
                node.pending_pings[node.sequence_number] = time.time()
            except:
                pass
            sock.sendto(pkt, (n_ip, DEFAULT_PORT))
            # Route Request
            req = json.dumps({'cmd': 'route_request'}).encode('utf-8')
            sock.sendto(node.pack_message(MsgType.DEBUG, n_ip, req), (n_ip, DEFAULT_PORT))
        except: pass

    # 4. Configuração de Multimédia
    ffmpeg_source = None
    ffplay_sink = None
    current_video_file = None
    last_downstream_log = 0
    DOWNSTREAM_LOG_INTERVAL = 5.0
    
    if args.node_id == "STREAMER1": current_video_file = "trailer_the_boys.mp4"
    elif args.node_id == "STREAMER2": current_video_file = "gta_vi_trailer.mp4"

    if "STREAMER" in args.node_id:
        if current_video_file and os.path.exists(current_video_file):
            print(f"[STREAMER] Ficheiro pronto: {current_video_file}")
        else:
            print(f"[ERRO] Vídeo não encontrado: {current_video_file}")
            
    if "C" in args.node_id: ffplay_sink = FFplayPlayer(title=args.node_id)

    # 5. Inicialização de Estado e Temporizadores
    join_state = {'active': False, 'stream_id': None, 'target_ip': None, 'last_sent': 0, 'retries': 0, 'parent_ip': None, 'last_failure': 0}
    
    last_hello = time.time() 
    last_flood = time.time()
    last_report = 0
    last_monitor_update = 0
    last_upstream_maint = 0
    last_tree_check = 0
    last_neighbor_check = 0
    last_frame_received_time = 0
    last_quality_switch = 0 
    last_route_panic = 0
    
    HELLO_INTERVAL = 1.0   
    FLOOD_INTERVAL = 1.0   
    JOIN_TIMEOUT = 2.5
    NEIGHBOR_CHECK_INTERVAL = 1.5
    NEIGHBOR_TIMEOUT = 5.0
    TREE_CHECK_INTERVAL = 2.0
    
    inputs = [sock, sys.stdin]
    frame_seq = 0
    stats_frames_received = 0
    stats_frames_lost = 0  
    last_seq_received = -1

    print("[*] Sistema pronto. (Digite 'join STREAMER1' para ver)")
    
    # 6. Loop Principal
    try:
        while True:
            try:
                now = time.time()
                
                # --- 1. JOIN Retry & Logica de Conexão ---
                if join_state.get('active'):
                    if join_state.get('last_failure', 0) > 0 and now - join_state['last_failure'] < RETRY_WAIT_INTERVAL:
                        pass # Backoff
                    elif now - join_state['last_sent'] > JOIN_TIMEOUT:
                        if join_state['retries'] < 3: 
                            # Retry Normal
                            print(f"[⏳] Retry JOIN para {join_state['target_ip']} ({join_state['retries'] + 1})")
                            pl = json.dumps({"stream_id": join_state['stream_id']}).encode('utf-8')
                            pk = node.pack_message(MsgType.STREAM_JOIN, join_state['target_ip'], pl)
                            sock.sendto(pk, (join_state['target_ip'], DEFAULT_PORT))
                            join_state['last_sent'] = now; join_state['retries'] += 1
                        else:
                            # Falha Definitiva - Procurar Rota Alternativa
                            print(f"[❌] Timeout JOIN para {join_state['target_ip']}")
                            sid = join_state['stream_id']
                            found_alt = False
                            if sid in node.routing_table:
                                new_nh = node.routing_table[sid].proximo_salto_ip
                                if new_nh != join_state['target_ip'] and new_nh != "SELF":
                                    print(f"[🔄] Alternativa encontrada: {new_nh}")
                                    join_state['target_ip'] = new_nh
                                    join_state['retries'] = 0
                                    join_state['last_failure'] = 0
                                    found_alt = True
                                    # Enviar imediato
                                    pl = json.dumps({"stream_id": sid}).encode('utf-8')
                                    sock.sendto(node.pack_message(MsgType.STREAM_JOIN, new_nh, pl), (new_nh, DEFAULT_PORT))
                                    join_state['last_sent'] = now
                            
                            if not found_alt:
                                print(f"[💤] Esperando {RETRY_WAIT_INTERVAL}s antes de tentar de novo...")
                                join_state['active'] = False
                                join_state['last_failure'] = now
                                join_state['retries'] = 0

                # --- 1.4. VERIFICAR VIZINHOS MORTOS ---
                if now - last_neighbor_check >= NEIGHBOR_CHECK_INTERVAL:
                    try:
                        dead_neighbors, updated_routes = node.check_dead_neighbors(timeout=NEIGHBOR_TIMEOUT)
                        if dead_neighbors:
                            print(f"[☠️] Mortos detectados: {dead_neighbors}")
                            
                            # Se o meu PAI morreu
                            if "C" in args.node_id and join_state.get('parent_ip') in dead_neighbors:
                                print(f"[🚨] PAI MORREU! Reiniciando processo de join.")
                                join_state['parent_ip'] = None
                                join_state['active'] = False
                                join_state['last_failure'] = 0 # Permitir retry imediato
                                
                                # Tentar reconexão imediata se houver rota
                                sid = join_state.get('stream_id')
                                if sid and sid in node.routing_table:
                                    nh = node.routing_table[sid].proximo_salto_ip
                                    if nh and nh != "SELF":
                                        join_state['active'] = True
                                        join_state['target_ip'] = nh
                                        join_state['retries'] = 0
                                        join_state['last_sent'] = 0 # Forçar envio no prox loop

                            # Se sou Streamer, forçar flood para limpar rotas
                            if "STREAMER" in args.node_id:
                                last_flood = 0 # Forçar flood no próximo ciclo

                    except Exception as e: print(f"Erro check_dead: {e}")
                    last_neighbor_check = now
            
                # --- 1.5 MONITORIZAÇÃO DE FLUXO (CLIENTES) ---
                if "C" in args.node_id and join_state.get('stream_id'):
                    # Se tenho pai mas não recebo frames há muito tempo
                    if join_state.get('parent_ip') and last_frame_received_time > 0:
                        if now - last_frame_received_time > 4.0:
                            print(f"[🚨] Sem frames há 4s. Resetando conexão.")
                            join_state['parent_ip'] = None
                            join_state['active'] = False
                            join_state['last_failure'] = 0

                    # Auto-Reconexão se desconectado
                    if not join_state.get('parent_ip') and not join_state.get('active'):
                         # Respeitar backoff
                        if not (join_state.get('last_failure', 0) > 0 and now - join_state['last_failure'] < RETRY_WAIT_INTERVAL):
                            sid = join_state.get('stream_id')
                            if sid and sid in node.routing_table:
                                nh = node.routing_table[sid].proximo_salto_ip
                                print(f"[🔌 AUTO] Reconectando a {sid} via {nh}")
                                join_state['active'] = True
                                join_state['target_ip'] = nh
                                join_state['retries'] = 0
                                join_state['last_sent'] = 0

                # --- 1.6 OTIMIZAÇÃO DE ÁRVORE ---
                if join_state.get('stream_id') and join_state.get('parent_ip') and now - last_tree_check >= TREE_CHECK_INTERVAL:
                    sid = join_state['stream_id']
                    if sid in node.routing_table:
                        best_nh = node.routing_table[sid].proximo_salto_ip
                        curr_parent = join_state['parent_ip']
                        
                        # Se a tabela diz que há um salto melhor que o pai atual
                        if best_nh != curr_parent and best_nh != "SELF":
                            # Verifica se a melhoria é significativa ou estável
                            entry = node.routing_table[sid]
                            # (Simples: se tabela mudou, muda também)
                            print(f"[REPARO 🔄] Melhor rota via {best_nh}. Mudando...")
                            
                            # Leave old
                            sock.sendto(node.pack_message(MsgType.STREAM_LEAVE, curr_parent, json.dumps({"stream_id": sid}).encode('utf-8')), (curr_parent, DEFAULT_PORT))
                            # Join new
                            join_state['active'] = True
                            join_state['target_ip'] = best_nh
                            join_state['retries'] = 0
                            join_state['last_sent'] = 0 # Envia já
                    last_tree_check = now

                # --- 2. HELLOS ---
                if now - last_hello >= HELLO_INTERVAL:
                    for n_ip in list(node.neighbors.keys()):
                        pkt = node.pack_message(MsgType.HELLO, n_ip, b"")
                        try:
                            node.pending_pings[node.sequence_number] = time.time()
                        except:
                            pass
                        sock.sendto(pkt, (n_ip, DEFAULT_PORT))
                    last_hello = now

                # --- 3. FLOOD (Streamer) ---
                if "STREAMER" in args.node_id and now - last_flood >= FLOOD_INTERVAL:
                    flood_pl = json.dumps({"stream_id": args.node_id, "cost": 0, "origin_seq": int(now*1000)}).encode('utf-8')
                    for n_ip, info in node.neighbors.items():
                        if info.get('state') == 'alive':
                            sock.sendto(node.pack_message(MsgType.ROUTE_DISCOVERY, n_ip, flood_pl), (n_ip, DEFAULT_PORT))
                    last_flood = now

                # --- 4. MONITORIZAÇÃO (Tracker) ---
                if now - last_monitor_update >= 2.0:
                    try:
                        r_data = {}
                        for sid, entry in node.routing_table.items():
                            r_data[sid] = {'next_hop': entry.proximo_salto_ip, 'cost': entry.custo_acumulado, 'downstream': list(entry.downstream_ips)}
                        update = json.dumps({
                            'node_id': args.node_id, 'ips': ips,
                            'neighbors': {k: v.get('metric') for k,v in node.neighbors.items()},
                            'routing_table': r_data, 'streams': list(node.routing_table.keys())
                        }).encode('utf-8')
                        sock.sendto(update, (args.tracker, MONITOR_PORT))
                    except: pass
                    last_monitor_update = now

                # --- 5. MANUTENÇÃO UPSTREAM ---
                if now - last_upstream_maint >= 2.0:
                    for sid, entry in node.routing_table.items():
                        if args.node_id != sid and entry.proximo_salto_ip != "SELF":
                            # Se tenho clientes OU estou a consumir
                            consuming = (ffplay_sink is not None and join_state.get('stream_id') == sid)
                            if entry.downstream_ips or consuming:
                                pl = json.dumps({"stream_id": sid}).encode('utf-8')
                                sock.sendto(node.pack_message(MsgType.STREAM_JOIN, entry.proximo_salto_ip, pl), (entry.proximo_salto_ip, DEFAULT_PORT))
                    last_upstream_maint = now

                # --- 6. QoS ---
                if "C" in args.node_id and now - last_report >= 2.0:
                    if stats_frames_received > 0:
                        expect = last_seq_received - (last_seq_received - stats_frames_received) # Aproximação
                        # Simples loss report
                        pl = json.dumps({"stream_id": join_state['stream_id'], "client_id": args.node_id, "loss_rate": 0}).encode('utf-8') # Simplificado
                        if join_state.get('parent_ip'):
                            sock.sendto(node.pack_message(MsgType.STREAM_REPORT, join_state['parent_ip'], pl), (join_state['parent_ip'], DEFAULT_PORT))
                        stats_frames_received = 0
                    last_report = now

                # --- 7. STREAMER SEND ---
                if "STREAMER" in args.node_id:
                    has_clients = False
                    if args.node_id in node.routing_table and node.routing_table[args.node_id].downstream_ips:
                        has_clients = True
                    
                    if has_clients and ffmpeg_source is None:
                        ffmpeg_source = FFmpegStreamer(current_video_file, quality='HIGH')
                        last_quality_switch = time.time()
                    elif not has_clients and ffmpeg_source is not None:
                        ffmpeg_source.close(); ffmpeg_source = None

                    if ffmpeg_source:
                        raw = ffmpeg_source.read_chunk(CHUNK_SIZE)
                        if raw:
                            frame_seq += 1
                            b64 = base64.b64encode(raw).decode('utf-8')
                            pl = json.dumps({"id": args.node_id, "seq": frame_seq, "data": b64}).encode('utf-8')
                            if args.node_id in node.routing_table:
                                for c in node.routing_table[args.node_id].downstream_ips:
                                    sock.sendto(node.pack_message(MsgType.STREAM_DATA, c, pl), (c, DEFAULT_PORT))
                                    time.sleep(0.001) # Pacing leve
                        elif raw == b'': 
                            ffmpeg_source.close(); ffmpeg_source = None

            except Exception as e:
                print(f"Erro Loop: {e}")
                time.sleep(1)

            # --- EVENT LOOP IO ---
            readable, _, _ = select.select(inputs, [], [], 0.005)
            for s in readable:
                if s is sock:
                    cnt = 0
                    while cnt < 50: # Evitar bloquear muito tempo
                        try:
                            data, addr = sock.recvfrom(MAX_PACKET_SIZE)
                            cnt += 1
                            sender_ip = addr[0]
                            
                            # Registo básico de vizinho
                            if sender_ip not in node.neighbors:
                                
                                if sender_ip != args.tracker:

                                    pass
                                continue

                            if node.neighbors[sender_ip]['state'] == 'dead':
                                print(f"[{args.node_id}] 🧟 VIZINHO RESSUSCITOU: {sender_ip} voltou a falar!")

                            node.neighbors[sender_ip]['last_seen'] = time.time()
                            node.neighbors[sender_ip]['state'] = 'alive'
                           

                            # 1. Tracker Messages (JSON)
                            try:
                                note = json.loads(data.decode('utf-8'))
                                if note.get('type') == 'neighbor_update':
                                    nip = note.get('new_neighbor')
                                    if nip and nip not in node.neighbors:
                                        try:
                                            rtt_n = measure_rtt(nip, timeout=1)
                                        except:
                                            rtt_n = None
                                        node.neighbors[nip] = {'metric':rtt_n, 'last_seen':time.time(), 'state':'alive'}
                                        pkt = node.pack_message(MsgType.HELLO, nip, b"")
                                        try:
                                            node.pending_pings[node.sequence_number] = time.time()
                                        except:
                                            pass
                                        sock.sendto(pkt, (nip, DEFAULT_PORT))
                                    continue
                                if note.get('type') == 'neighbor_batch':
                                    for nip in note.get('neighbors', []):
                                        if nip and nip not in node.neighbors:
                                            try:
                                                rtt_n = measure_rtt(nip, timeout=1)
                                            except:
                                                rtt_n = None
                                            node.neighbors[nip] = {'metric':rtt_n, 'last_seen':time.time(), 'state':'alive'}
                                    continue
                            except: pass

                            # 2. Overlay Messages (Binary)
                            try:
                                header, payload = node.unpack_message(data)
                            except: continue
                            
                            if not header: continue
                            
                            mtype = header['type']
                            
                            if mtype == MsgType.HELLO:
                                sock.sendto(node.pack_message(MsgType.HELLO_RESPONSE, sender_ip, node.handle_hello(header, sender_ip)), (sender_ip, DEFAULT_PORT))
                            
                            elif mtype == MsgType.HELLO_RESPONSE:
                                node.handle_hello_response(payload, sender_ip)
                            
                            elif mtype == MsgType.ROUTE_DISCOVERY:
                                was_new = sender_ip not in node.neighbors
                                new_pl = node.handle_flood(header, payload, sender_ip)
                                if was_new: # Se descobri vizinho via flood, digo olá
                                    sock.sendto(node.pack_message(MsgType.HELLO, sender_ip, b""), (sender_ip, DEFAULT_PORT))

                                if new_pl:
                                    # Forward
                                    for n in node.neighbors:
                                        if n != sender_ip and node.neighbors[n].get('state') == 'alive':
                                            sock.sendto(node.pack_message(MsgType.ROUTE_DISCOVERY, n, new_pl), (n, DEFAULT_PORT))
                                    
                                    # Trigger Join se sou cliente à espera deste stream
                                    if "C" in args.node_id and join_state.get('stream_id'):
                                        f_info = json.loads(payload)
                                        if f_info.get('stream_id') == join_state['stream_id']:
                                            # Se não tenho pai, ou se a rota mudou
                                            if not join_state.get('parent_ip'):
                                                # Tentar conectar via quem enviou o flood se fizer sentido (simplificação)
                                                # O ideal é usar a routing table que acabou de ser atualizada pelo handle_flood
                                                if f_info['stream_id'] in node.routing_table:
                                                    nh = node.routing_table[f_info['stream_id']].proximo_salto_ip
                                                    if nh != "SELF":
                                                        print(f"[⚡ FLOOD] Rota encontrada! Conectando a {nh}")
                                                        join_state['active'] = True; join_state['target_ip'] = nh; join_state['retries'] = 0; join_state['last_sent'] = 0
                                
                                if was_new: # Se descobri vizinho via flood, digo olá
                                    pkt = node.pack_message(MsgType.HELLO, sender_ip, b"")
                                    try:
                                        node.pending_pings[node.sequence_number] = time.time()
                                    except:
                                        pass
                                    sock.sendto(pkt, (sender_ip, DEFAULT_PORT))

                            elif mtype == MsgType.STREAM_JOIN:
                                up, ack, _ = node.handle_join(payload, sender_ip)
                                if ack:
                                    ack_pl = json.dumps({"stream_id": json.loads(payload).get('stream_id')}).encode('utf-8')
                                    sock.sendto(node.pack_message(MsgType.ACK_JOIN, sender_ip, ack_pl), (sender_ip, DEFAULT_PORT))
                                if up and up!="SOURCE":
                                    sock.sendto(node.pack_message(MsgType.STREAM_JOIN, up, payload), (up, DEFAULT_PORT))
                                if up is None:
                                # Só entra em pânico se já passaram 2 segundos desde o último grito
                                    if now - last_route_panic > 2.0:
                                        print(f"[{args.node_id}] 🆘 Sem rota para atender JOIN! Pedindo rotas aos vizinhos...")
                                        req = json.dumps({'cmd': 'route_request'}).encode('utf-8')
                                        for n_ip, info in node.neighbors.items():
                                            if n_ip != sender_ip and info.get('state') == 'alive':
                                                sock.sendto(node.pack_message(MsgType.DEBUG, n_ip, req), (n_ip, DEFAULT_PORT))
                                        last_route_panic = now


                            elif mtype == MsgType.ACK_JOIN:
                                if join_state['active'] and sender_ip == join_state['target_ip']:
                                    join_state['active'] = False
                                    join_state['parent_ip'] = sender_ip
                                    last_frame_received_time = time.time()
                                    print(f"[✅] CONECTADO a {sender_ip}")

                            elif mtype == MsgType.STREAM_LEAVE:
                                up = node.handle_leave(payload, sender_ip)
                                if up and up!="SOURCE": sock.sendto(node.pack_message(MsgType.STREAM_LEAVE, up, payload), (up, DEFAULT_PORT))

                            elif mtype == MsgType.STREAM_REPORT:
                                up = node.handle_report(payload, sender_ip)
                                if up and up!="SOURCE": 
                                    sock.sendto(node.pack_message(MsgType.STREAM_REPORT, up, payload), (up, DEFAULT_PORT))
                                elif up == "SOURCE" and ffmpeg_source:
                                    # QoS Lógica
                                    try:
                                        l = json.loads(payload).get('loss_rate', 0.0)
                                        if time.time() - last_quality_switch > 15.0:
                                            if l > 10.0 and ffmpeg_source.current_quality == 'HIGH':
                                                ffmpeg_source.close(); ffmpeg_source = FFmpegStreamer(current_video_file, 'LOW'); last_quality_switch = time.time()
                                                print("[QoS] Baixando qualidade (Perda alta)")
                                            elif l < 2.0 and ffmpeg_source.current_quality == 'LOW':
                                                ffmpeg_source.close(); ffmpeg_source = FFmpegStreamer(current_video_file, 'HIGH'); last_quality_switch = time.time()
                                                print("[QoS] Subindo qualidade (Rede boa)")
                                    except: pass

                            elif mtype == MsgType.STREAM_DATA:
                                # Data Forwarding
                                info = json.loads(payload); sid = info.get('id')
                                if sid in node.routing_table:
                                    for c in node.routing_table[sid].downstream_ips:
                                        if c != sender_ip:
                                            sock.sendto(node.pack_message(MsgType.STREAM_DATA, c, payload), (c, DEFAULT_PORT))
                                # Playback
                                if ffplay_sink:
                                    last_frame_received_time = time.time()
                                    ffplay_sink.write_data(base64.b64decode(info.get('data')))
                                    stats_frames_received += 1


                            # ... (dentro do loop while do main.py) ...

                            elif mtype == MsgType.ROUTE_REPLY:
                                # O R3 recebe a resposta do R7 aqui!
                                try:
                                    # Descodifica a resposta (JSON com as rotas do vizinho)
                                    reply_data = json.loads(payload.decode('utf-8'))
                                    routes = reply_data.get('routes', {})
                                    
                                    # Custo do link para este vizinho
                                    metric = node.get_link_metric(sender_ip)
                                    
                                    updated_count = 0
                                    
                                    # Analisa as rotas que o vizinho mandou
                                    for stream_id, r_info in routes.items():
                                        cost_via_neighbor = r_info.get('cost', float('inf')) + metric
                                        
                                        # Se não temos rota OU esta rota é melhor, atualizamos!
                                        if stream_id not in node.routing_table or cost_via_neighbor < node.routing_table[stream_id].custo_acumulado:
                                            # IMPORTANTE: Importar RouteEntry se não estiver disponível
                                            from overlay_structs import RouteEntry 
                                            
                                            node.routing_table[stream_id] = RouteEntry(stream_id, sender_ip, cost_via_neighbor)
                                            node.routing_table[stream_id].last_update = time.time()
                                            updated_count += 1
                                            print(f"[{args.node_id}] 💡 Aprendi rota para {stream_id} via {sender_ip} (custo {cost_via_neighbor:.1f})")
                                    
                                except Exception as e:
                                    print(f"Erro ao processar ROUTE_REPLY: {e}")

                            elif mtype == MsgType.DEBUG:
                                # Alguém (ex: R3) está a pedir-me as minhas rotas!
                                resp = node.handle_debug(header, payload, sender_ip)
                                
                                # Se o handle_debug gerou uma resposta, tenho de a ENVIAR
                                if resp:
                                    print(f"[{args.node_id}] 🚑 Enviando socorro (rotas) para {sender_ip}")
                                    sock.sendto(node.pack_message(MsgType.ROUTE_REPLY, sender_ip, resp), (sender_ip, DEFAULT_PORT))
                                    
                        except Exception: 
                            break

                elif s is sys.stdin:
                    cmd = sys.stdin.readline().strip()
                    if cmd == "status":
                        print(f"--- {args.node_id} ---")
                        print(f"Vizinhos: {len(node.neighbors)}")
                        for k,v in node.neighbors.items():
                            m = v.get('metric')
                            if m is None:
                                m_disp = '(?)'
                            else:
                                try:
                                    m_disp = f"{m:.1f}ms"
                                except:
                                    m_disp = str(m)
                            print(f"  {k}: {v.get('state')} {m_disp}")
                        for sid, r in node.routing_table.items(): 
                            print(f"Rota {sid}: via {r.proximo_salto_ip} (custo {r.custo_acumulado:.1f}) Clientes: {r.downstream_ips}")
                        if join_state.get('stream_id'):
                            print(f"Stream Alvo: {join_state['stream_id']} | Pai: {join_state.get('parent_ip')} | Frames: {stats_frames_received}")
                        # Estatísticas de segurança
                        if node.security.is_enabled():
                            print(f"🔒 Segurança: ATIVA | Enviados: {node.stats_encrypted_sent} | Recebidos: {node.stats_encrypted_recv} | Falhas: {node.stats_decrypt_failed}")
                        else:
                            print(f"🔓 Segurança: DESATIVADA (modo debug)")
                    
                    elif cmd.startswith("join"):
                        if "C" in args.node_id and ffplay_sink is None:
                            ffplay_sink = FFplayPlayer(title=args.node_id)
                        
                        parts = cmd.split()
                        if len(parts) > 1:
                            tgt = parts[1]
                            if tgt in node.routing_table:
                                nh = node.routing_table[tgt].proximo_salto_ip
                                print(f"[JOIN] Iniciando join a {tgt} via {nh}")
                                pl = json.dumps({"stream_id": tgt}).encode('utf-8')
                                sock.sendto(node.pack_message(MsgType.STREAM_JOIN, nh, pl), (nh, DEFAULT_PORT))
                                join_state = {'active':True, 'stream_id':tgt, 'target_ip':nh, 'last_sent':time.time(), 'retries':0, 'parent_ip':None, 'last_failure':0}
                            else:
                                print("[!] Rota desconhecida. Aguarde flood.")
                        else:
                            print("Rotas disponíveis:", list(node.routing_table.keys()))


                    elif cmd.startswith("leave"):
                        parts = cmd.split()
                        # Se o utilizador não disser qual, assumimos o atual
                        sid_to_leave = parts[1] if len(parts) > 1 else join_state.get('stream_id')
                        
                        if not sid_to_leave:
                            print("Uso: leave <stream_id>")
                            continue

                        # Verificar se estamos a tentar sair do stream correto
                        if join_state.get('stream_id') == sid_to_leave:
                            print(f"[{args.node_id}] 👋 A sair do stream {sid_to_leave}...")
                            
                            # 1. Avisar o pai (se tivermos um)
                            parent = join_state.get('parent_ip')
                            if parent:
                                pl = json.dumps({"stream_id": sid_to_leave}).encode('utf-8')
                                # Envia STREAM_LEAVE para o pai
                                sock.sendto(node.pack_message(MsgType.STREAM_LEAVE, parent, pl), (parent, DEFAULT_PORT))
                            
                            # 2. Limpar o estado local
                            join_state['active'] = False
                            join_state['stream_id'] = None
                            join_state['target_ip'] = None
                            join_state['parent_ip'] = None
                            join_state['retries'] = 0
                            
                            # Parar o player visual se estiver a correr
                            if ffplay_sink:
                                ffplay_sink.close() # Fecha a janela visual
                                ffplay_sink = None  # Apaga o objeto para recriar depois
                            
                            print(f"[{args.node_id}] ✅ Desconectado e janela fechada.")

                        else:
                            print(f"[{args.node_id}] ⚠️ Não estás conectado ao stream '{sid_to_leave}'.")

    except KeyboardInterrupt: print("\nBye.")
    finally:
        if ffmpeg_source: ffmpeg_source.close()
        sock.close()

if __name__ == "__main__": main()