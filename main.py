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
        node.neighbors[neighbor_ip] = {'metric': 50.0, 'last_seen': time.time(), 'state': 'alive', 'missed_hellos': 0}
        # Pedir ao vizinho um snapshot de rotas para integrar rapidamente
        try:
            req = json.dumps({'cmd': 'route_request'}).encode('utf-8')
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(node.pack_message(MsgType.DEBUG, neighbor_ip, req), (neighbor_ip, DEFAULT_PORT))
            sock.close()
        except: pass

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
            # CORREÇÃO CRÍTICA: STREAMER precisa de entrada na routing_table para enviar dados
            from overlay_structs import RouteEntry
            node.routing_table[args.node_id] = RouteEntry(args.node_id, "SELF", 0.0)
            print(f"[*] STREAMER inicializado: {args.node_id} pronto para transmitir")
        else:
            print(f"[ERRO] Vídeo não encontrado: {current_video_file}")
            
    if "C" in args.node_id: ffplay_sink = FFplayPlayer()

    # Estado
    join_state = {'active': False, 'stream_id': None, 'target_ip': None, 'last_sent': 0, 'retries': 0, 'parent_ip': None}
    last_hello = 0
    last_flood = 0
    last_report = 0        
    last_monitor_update = 0
    last_tree_check = 0
    last_neighbor_check = 0
    
    HELLO_INTERVAL = 1.0 
    FLOOD_INTERVAL = 5.0  # Reduzido para propagar mudanças mais rápido
    JOIN_TIMEOUT = 5.0
    MONITOR_UPDATE_INTERVAL = 2.0
    TREE_CHECK_INTERVAL = 2.0  # Verificação rápida da árvore
    NEIGHBOR_CHECK_INTERVAL = 8.0  # Verifica vizinhos mortos a cada 8s
    NEIGHBOR_TIMEOUT = 20.0  # 20 HELLOs perdidos = morto (CONSERVADOR)

    inputs = [sock, sys.stdin]
    frame_seq = 0
    stats_frames_received = 0
    stats_frames_lost = 0  
    last_seq_received = -1
    last_frame_received_time = 0.0  # Para detetar perda de stream
    
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
                        print(f"[❌] Falha no JOIN após 3 tentativas.")
                        # Tentar encontrar rota alternativa
                        if join_state['stream_id'] in node.routing_table:
                            new_best_nh = node.routing_table[join_state['stream_id']].proximo_salto_ip
                            if new_best_nh != join_state['target_ip']:
                                print(f"[🔄] Tentando rota alternativa via {new_best_nh}")
                                join_state['target_ip'] = new_best_nh
                                pl = json.dumps({"stream_id": join_state['stream_id']}).encode('utf-8')
                                pkt = node.pack_message(MsgType.STREAM_JOIN, join_state['target_ip'], pl)
                                sock.sendto(pkt, (join_state['target_ip'], DEFAULT_PORT))
                                join_state['last_sent'] = now
                                join_state['retries'] = 0  # Reset retries
                            else:
                                join_state['active'] = False
                        else:
                            join_state['active'] = False
            
            # --- 1.4. VERIFICAR VIZINHOS MORTOS (ATIVADO) ---
            if now - last_neighbor_check >= NEIGHBOR_CHECK_INTERVAL:
                dead_neighbors = node.check_dead_neighbors(timeout=NEIGHBOR_TIMEOUT)
                if dead_neighbors:
                    print(f"[☠️] {len(dead_neighbors)} vizinho(s) morto(s) detectado(s)!")
                    
                    # CLIENTES: Se pai morreu, forçar desconexão imediata
                    if "C" in args.node_id and join_state.get('parent_ip') in dead_neighbors:
                        print(f"[🚨 PAI MORTO] {join_state['parent_ip']} não responde!")
                        join_state['parent_ip'] = None
                        join_state['active'] = False
                        print(f"[🔍] Procurando rota alternativa...")
                    
                    # FORÇAR RE-FLOOD para atualizar rotas (STREAMERS)
                    if "STREAMER" in args.node_id:
                        print(f"[{args.node_id}] 🔄 Forçando RE-FLOOD após perda de vizinho")
                        flood_payload = json.dumps({
                            "stream_id": args.node_id, "cost": 0, "origin_seq": int(now * 1000)
                        }).encode('utf-8')
                        for n_ip, info in node.neighbors.items():
                            if info.get('state', 'alive') == 'alive':
                                pkt = node.pack_message(MsgType.ROUTE_DISCOVERY, n_ip, flood_payload)
                                sock.sendto(pkt, (n_ip, DEFAULT_PORT))
                        last_flood = now
                    
                    # ROUTERS: Re-propagar rotas que ainda temos
                    else:
                        routes_to_repropagate = []
                        for stream_id, entry in node.routing_table.items():
                            if entry.proximo_salto_ip != "SELF":  # Não propagar se formos a fonte
                                routes_to_repropagate.append((stream_id, entry.custo_acumulado))
                        
                        if routes_to_repropagate:
                            print(f"[{args.node_id}] 🔄 Re-propagando {len(routes_to_repropagate)} rota(s) via caminhos alternativos")
                            for stream_id, cost in routes_to_repropagate:
                                # Usar timestamp muito granular para garantir origin_seq único
                                unique_seq = int(time.time() * 1000000) % 2147483647
                                flood_payload = json.dumps({
                                    "stream_id": stream_id, "cost": cost, "origin_seq": unique_seq
                                }).encode('utf-8')
                                for n_ip, info in node.neighbors.items():
                                    if n_ip not in dead_neighbors and info.get('state', 'alive') == 'alive':  # Não enviar para mortos
                                        pkt = node.pack_message(MsgType.ROUTE_DISCOVERY, n_ip, flood_payload)
                                        sock.sendto(pkt, (n_ip, DEFAULT_PORT))
                                        print(f"[{args.node_id}]   → Enviando para {n_ip} (custo {cost:.2f})")
                                time.sleep(0.01)  # Pequeno delay entre floods
                
                last_neighbor_check = now
            
            # --- 1.5. DETEÇÃO E RECUPERAÇÃO AUTOMÁTICA (CLIENTES) ---
            # Executado SEMPRE (não depende de intervalo)
            if "C" in args.node_id and join_state.get('stream_id'):
                stream_id = join_state['stream_id']
                current_parent = join_state.get('parent_ip')
                is_connecting = join_state.get('active', False)
                
                # CASO A: Estava a receber frames mas já não recebe (CRÍTICO!)
                # Verifica SEMPRE, independentemente de ter pai ou não
                if last_frame_received_time > 0:
                    time_without_frames = now - last_frame_received_time
                    # Se parou de receber frames E tem pai marcado → RESETAR!
                    if time_without_frames > 4.0 and current_parent:
                        print(f"[🚨 CONEXÃO PERDIDA] Sem frames há {time_without_frames:.1f}s!")
                        print(f"[🔄] Pai {current_parent} pode estar morto, resetando...")
                        join_state['parent_ip'] = None
                        join_state['active'] = False
                        # NÃO resetar last_frame_received_time aqui - mantém para tracking
                
                # CASO B: Não tem pai conectado → TENTAR RECONECTAR
                if not current_parent and not is_connecting:
                    # Verifica se tem rota disponível
                    if stream_id in node.routing_table:
                        entry = node.routing_table[stream_id]
                        new_next_hop = entry.proximo_salto_ip
                        
                        # Debounce: esperar 1 segundo entre tentativas
                        time_since_last = now - join_state.get('last_sent', 0)
                        if time_since_last > 1.0:
                            print(f"[🔌 AUTO-RECONEXÃO] Rota encontrada para {stream_id}")
                            print(f"    Via: {new_next_hop} (custo {entry.custo_acumulado:.2f})")
                            
                            # Enviar JOIN
                            pl_join = json.dumps({"stream_id": stream_id}).encode('utf-8')
                            pkt_join = node.pack_message(MsgType.STREAM_JOIN, new_next_hop, pl_join)
                            sock.sendto(pkt_join, (new_next_hop, DEFAULT_PORT))
                            
                            join_state['active'] = True
                            join_state['target_ip'] = new_next_hop
                            join_state['last_sent'] = now
                            join_state['retries'] = 0
                    else:
                        # Sem rota: Log periódico e procurar ativamente
                        time_since_last = now - join_state.get('last_sent', 0)
                        if time_since_last > 3.0:  # A cada 3 segundos (mais frequente)
                            # Verificar se há ALGUMA rota para QUALQUER stream
                            available_streams = len(node.routing_table)
                            if available_streams > 0:
                                print(f"[⚠️] DESCONECTADO de {stream_id}, mas {available_streams} stream(s) disponível(eis)")
                                # Mostrar alternativas
                                for sid, e in list(node.routing_table.items())[:3]:  # máximo 3
                                    print(f"    • {sid}: custo {e.custo_acumulado:.2f} via {e.proximo_salto_ip}")
                            else:
                                print(f"[⚠️] DESCONECTADO: Aguardando FLOODs de {stream_id}...")
                            join_state['last_sent'] = now
            
            # --- 1.6. REPARAÇÃO DA ÁRVORE (Tree Maintenance) ---
            # Verifica mudanças de rota e otimizações
            if join_state.get('stream_id') and now - last_tree_check >= TREE_CHECK_INTERVAL:
                stream_id = join_state.get('stream_id')
                current_parent = join_state.get('parent_ip')
                
                # CASO 1: Temos pai mas perdemos a rota (pai morreu)
                if current_parent and stream_id not in node.routing_table:
                    print(f"[🚨 FAILOVER] Rota para {stream_id} PERDIDA! Pai {current_parent} morreu")
                    # Resetar estado - a lógica de auto-reconexão vai tratar
                    join_state['parent_ip'] = None
                    join_state['active'] = False
                    print(f"[🔍] Aguardando novos FLOODs...")
                
                # CASO 2: Temos pai E rota, mas o next hop mudou (caminho melhor)
                elif current_parent and stream_id in node.routing_table:
                    entry = node.routing_table[stream_id]
                    new_best_next_hop = entry.proximo_salto_ip
                    
                    # A rota mudou significativamente?
                    if new_best_next_hop != current_parent and not join_state['active']:
                        # Verificar se a mudança é recente (últimos 2 intervalos de FLOOD)
                        time_since_update = now - entry.last_update
                        if time_since_update < (FLOOD_INTERVAL * 2):
                            print(f"[REPARO 🔄] Mudança de rota detectada para {stream_id}:")
                            print(f"    Pai antigo: {current_parent}")
                            print(f"    Novo NH: {new_best_next_hop} (custo {entry.custo_acumulado:.2f})")
                            
                            # 1. Enviar LEAVE ao antigo pai
                            pl_leave = json.dumps({"stream_id": stream_id}).encode('utf-8')
                            pkt_leave = node.pack_message(MsgType.STREAM_LEAVE, current_parent, pl_leave)
                            sock.sendto(pkt_leave, (current_parent, DEFAULT_PORT))
                            
                            # 2. Iniciar novo JOIN
                            pl_join = json.dumps({"stream_id": stream_id}).encode('utf-8')
                            pkt_join = node.pack_message(MsgType.STREAM_JOIN, new_best_next_hop, pl_join)
                            sock.sendto(pkt_join, (new_best_next_hop, DEFAULT_PORT))
                            
                            # 3. Atualizar estado
                            join_state['active'] = True
                            join_state['target_ip'] = new_best_next_hop
                            join_state['last_sent'] = now
                            join_state['retries'] = 0
                        
                last_tree_check = now


            # --- 2. HELLOS ---
            if now - last_hello >= HELLO_INTERVAL:
                num_sent = 0
                for n_ip in node.neighbors:
                    pkt = node.pack_message(MsgType.HELLO, n_ip, b"")
                    node.pending_pings[node.sequence_number] = now
                    sock.sendto(pkt, (n_ip, DEFAULT_PORT))
                    num_sent += 1
                # Debug periódico
                if int(now) % 10 == 0 and num_sent > 0:
                    print(f"[{args.node_id}] 💓 Enviando HELLOs para {num_sent} vizinhos")
                last_hello = now

            # --- 3. FLOOD (STREAMERS INICIAM) ---
            if "STREAMER" in args.node_id and now - last_flood >= FLOOD_INTERVAL:
                flood_payload = json.dumps({
                    "stream_id": args.node_id, "cost": 0, "origin_seq": int(now)
                }).encode('utf-8')
                num_sent = 0
                for n_ip, info in node.neighbors.items():
                    if info.get('state', 'alive') == 'alive':
                        pkt = node.pack_message(MsgType.ROUTE_DISCOVERY, n_ip, flood_payload)
                        sock.sendto(pkt, (n_ip, DEFAULT_PORT))
                        num_sent += 1
                if num_sent > 0:
                    print(f"[{args.node_id}] 📡 FLOOD enviado para {num_sent} vizinhos")
                else:
                    print(f"[{args.node_id}] ⚠️ FLOOD NÃO ENVIADO: sem vizinhos!")
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
                        elif frame_seq % 500 == 0:  # Debug a cada 500 frames
                            print(f"[STREAMER] Frame {frame_seq}: sem clientes downstream")
                    elif frame_seq % 500 == 0:
                        print(f"[STREAMER] Frame {frame_seq}: entrada routing_table não existe!")
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
                            # Batch inicial de vizinhos enviado pelo tracker após registo
                            if note.get('type') == 'neighbor_batch':
                                for new_ip in note.get('neighbors', []):
                                    if new_ip and new_ip not in node.neighbors:
                                        node.neighbors[new_ip] = {'metric': 50.0, 'last_seen': now, 'state': 'alive', 'missed_hellos': 0}
                                        print(f"[{args.node_id}] 👋 Vizinho (batch) adicionado: {new_ip}")
                                        pkt = node.pack_message(MsgType.HELLO, new_ip, b"")
                                        sock.sendto(pkt, (new_ip, DEFAULT_PORT))
                                continue

                            if note.get('type') == 'neighbor_update':
                                # O Tracker estrito já filtrou, então podemos confiar
                                new_ip = note.get('new_neighbor')
                                if new_ip and new_ip not in node.neighbors:
                                    # Inicializar com timestamp atual
                                    node.neighbors[new_ip] = {'metric': 50.0, 'last_seen': now, 'state': 'alive', 'missed_hellos': 0}
                                    print(f"[{args.node_id}] 👋 Novo vizinho: {new_ip}")
                                    pkt = node.pack_message(MsgType.HELLO, new_ip, b"")
                                    sock.sendto(pkt, (new_ip, DEFAULT_PORT))
                                continue
                        except: pass
                        
                        header, payload = node.unpack_message(data)
                        if not header: continue

                        if header['type'] == MsgType.HELLO:
                            sock.sendto(node.pack_message(MsgType.HELLO_RESPONSE, sender_ip_real, node.handle_hello(header, sender_ip_real)), (sender_ip_real, DEFAULT_PORT))
                        elif header['type'] == MsgType.DEBUG:
                            # Responder a pedidos debug/consulta (ex: route_request)
                            resp = node.handle_debug(header, payload, sender_ip_real)
                            if resp:
                                sock.sendto(node.pack_message(MsgType.ROUTE_REPLY, sender_ip_real, resp), (sender_ip_real, DEFAULT_PORT))
                        elif header['type'] == MsgType.ROUTE_REPLY:
                            # Incorporar snapshot de rotas vindo de um vizinho
                            try:
                                data = json.loads(payload.decode('utf-8'))
                                routes = data.get('routes', {})
                                metric = node.neighbors.get(sender_ip_real, {}).get('metric', 50.0)
                                updated = 0
                                for sid, info in routes.items():
                                    neigh_cost = info.get('cost', float('inf'))
                                    new_cost = neigh_cost + metric
                                    if sid not in node.routing_table or new_cost < node.routing_table[sid].custo_acumulado * 0.95:
                                        from overlay_structs import RouteEntry
                                        node.routing_table[sid] = RouteEntry(sid, sender_ip_real, new_cost)
                                        node.routing_table[sid].last_update = time.time()
                                        updated += 1
                                if updated > 0:
                                    print(f"[{args.node_id}] 🔁 Incorporadas {updated} rota(s) a partir de {sender_ip_real}")
                            except: pass
                        elif header['type'] == MsgType.HELLO_RESPONSE: node.handle_hello_response(payload, sender_ip_real)
                        elif header['type'] == MsgType.ROUTE_DISCOVERY:
                            # Verificar se aprendemos vizinho novo neste flood
                            was_new_neighbor = sender_ip_real not in node.neighbors
                            
                            new_pl = node.handle_flood(header, payload, sender_ip_real)
                            
                            # Se aprendemos vizinho novo, enviar HELLO imediato para confirmar link
                            if was_new_neighbor and sender_ip_real in node.neighbors:
                                print(f"[{args.node_id}] 👋 Enviando HELLO para confirmar vizinho descoberto {sender_ip_real}")
                                pkt_hello = node.pack_message(MsgType.HELLO, sender_ip_real, b"")
                                node.pending_pings[node.sequence_number] = now
                                sock.sendto(pkt_hello, (sender_ip_real, DEFAULT_PORT))
                            
                            if new_pl:
                                num_propagated = 0
                                for n, info in node.neighbors.items():
                                    if n != sender_ip_real and info.get('state', 'alive') == 'alive':
                                        sock.sendto(node.pack_message(MsgType.ROUTE_DISCOVERY, n, new_pl), (n, DEFAULT_PORT))
                                        num_propagated += 1
                            
                            # CRÍTICO: Se cliente recebeu FLOOD do stream que quer mas não tem pai → JOIN IMEDIATO!
                            if "C" in args.node_id and join_state.get('stream_id'):
                                try:
                                    flood_data = json.loads(payload)
                                    flood_stream_id = flood_data.get('stream_id')
                                    
                                    # É o stream que queremos E não temos pai?
                                    if flood_stream_id == join_state['stream_id'] and not join_state.get('parent_ip') and not join_state['active']:
                                        if flood_stream_id in node.routing_table:
                                            entry = node.routing_table[flood_stream_id]
                                            next_hop = entry.proximo_salto_ip
                                            print(f"[⚡ FLOOD TRIGGER] Recebido FLOOD de {flood_stream_id}, conectando VIA {next_hop}!")
                                            
                                            pl_join = json.dumps({"stream_id": flood_stream_id}).encode('utf-8')
                                            pkt_join = node.pack_message(MsgType.STREAM_JOIN, next_hop, pl_join)
                                            sock.sendto(pkt_join, (next_hop, DEFAULT_PORT))
                                            
                                            join_state['active'] = True
                                            join_state['target_ip'] = next_hop
                                            join_state['last_sent'] = now
                                            join_state['retries'] = 0
                                except: pass
                        elif header['type'] == MsgType.STREAM_JOIN:
                            up, ack, already_receiving = node.handle_join(payload, sender_ip_real)
                            
                            # Sempre enviar ACK se indicado
                            if ack:
                                try:
                                    sid = json.loads(payload)['stream_id']
                                    ack_payload = json.dumps({"stream_id": sid}).encode('utf-8')
                                    sock.sendto(node.pack_message(MsgType.ACK_JOIN, sender_ip_real, ack_payload), (sender_ip_real, DEFAULT_PORT))
                                except: pass
                            
                            # Propagar upstream APENAS se:
                            # 1. Tenho upstream (up != None)
                            # 2. Não sou a SOURCE (up != "SOURCE")
                            # 3. Este é o PRIMEIRO cliente (not already_receiving)
                            if up and up != "SOURCE" and not already_receiving:
                                print(f"[{args.node_id}] ⬆️ Propagando JOIN para upstream {up}")
                                sock.sendto(node.pack_message(MsgType.STREAM_JOIN, up, payload), (up, DEFAULT_PORT))
                            elif already_receiving:
                                print(f"[{args.node_id}] ✅ Já estou recebendo stream, não preciso propagar JOIN")
                        elif header['type'] == MsgType.ACK_JOIN:
                            if join_state['active'] and sender_ip_real == join_state['target_ip']: 
                                join_state['active'] = False
                                join_state['parent_ip'] = sender_ip_real
                                last_frame_received_time = now  # Reset do timer ao conectar
                                # Reset stats para nova conexão
                                stats_frames_received = 0
                                stats_frames_lost = 0
                                last_seq_received = -1
                                print(f"[✅ CONECTADO] Pai na Árvore: {join_state['parent_ip']}")
                                print(f"[🎥] Stream ativa! Aguardando frames...")


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
                                    
                                    # Log quando primeiro frame chega após reconexão
                                    if stats_frames_received == 0 or (last_seq_received == -1):
                                        print(f"[🎬 PRIMEIRO FRAME] Stream iniciada! (seq={seq})")
                                    
                                    last_seq_received = seq
                                    last_frame_received_time = now  # Marca timestamp de recepção
                                    ffplay_sink.write_data(base64.b64decode(info.get('data')))
                                    stats_frames_received += 1
                            except: pass
                        if packet_count > 100: break

                elif s is sys.stdin:
                    cmd = sys.stdin.readline().strip()
                    if cmd == "status":
                        print(f"\n=== STATUS {args.node_id} ===")
                        alive = sum(1 for i in node.neighbors.values() if i.get('state')=='alive')
                        lost = sum(1 for i in node.neighbors.values() if i.get('state')=='lost')
                        print(f"Vizinhos: {len(node.neighbors)} (alive={alive}, lost={lost})")
                        for n_ip, info in node.neighbors.items():
                            age = now - info.get('last_seen', 0)
                            state = info.get('state', 'unknown')
                            print(f"  - {n_ip}: {state} RTT={info.get('metric', 0):.1f}ms (visto há {age:.1f}s)")
                        
                        print(f"\nTabela de Roteamento: {len(node.routing_table)} entradas")
                        for sid, entry in node.routing_table.items():
                            print(f"  - {sid}: via {entry.proximo_salto_ip} (custo={entry.custo_acumulado:.1f}, clientes={len(entry.downstream_ips)})")
                        
                        if join_state.get('stream_id'):
                            stream_id = join_state['stream_id']
                            parent = join_state.get('parent_ip')
                            active = join_state['active']
                            
                            print(f"\nStream: {stream_id}")
                            if parent and not active:
                                # Verificar se pai ainda está vivo (state == 'alive')
                                parent_alive = node.neighbors.get(parent, {}).get('state') == 'alive'
                                if last_frame_received_time > 0:
                                    age = now - last_frame_received_time
                                    if age < 5.0:
                                        print(f"  Status: ✅ CONECTADO e ATIVO")
                                    else:
                                        print(f"  Status: ⚠️ CONECTADO mas SEM FRAMES ({age:.1f}s)")
                                else:
                                    print(f"  Status: 🔗 CONECTADO (aguardando frames)")
                                print(f"  Pai: {parent} {'✅ vivo' if parent_alive else '⚠️ morto/desconhecido'}")
                                print(f"  Frames recebidos: {stats_frames_received}")
                            elif active:
                                print(f"  Status: 🔄 CONECTANDO...")
                                print(f"  Target: {join_state.get('target_ip')}")
                                print(f"  Tentativas: {join_state.get('retries', 0)}")
                            else:
                                print(f"  Status: ❌ DESCONECTADO")
                                if stream_id in node.routing_table:
                                    entry = node.routing_table[stream_id]
                                    print(f"  Rota disponível: ✅ via {entry.proximo_salto_ip} (custo {entry.custo_acumulado:.1f})")
                                else:
                                    print(f"  Rota disponível: ❌ Não (aguardando FLOODs)")
                        print()
                    elif cmd.startswith("join"):
                        parts = cmd.split()
                        if len(parts) > 1:
                            target = parts[1]
                            if target in node.routing_table:
                                entry = node.routing_table[target]
                                nh = entry.proximo_salto_ip
                                cost = entry.custo_acumulado
                                
                                print(f"[🔌 JOIN] Stream: {target}")
                                print(f"    Via: {nh} (custo {cost:.2f})")
                                
                                pl = json.dumps({"stream_id": target}).encode('utf-8')
                                sock.sendto(node.pack_message(MsgType.STREAM_JOIN, nh, pl), (nh, DEFAULT_PORT))
                                
                                # Reset do estado de JOIN, mas 'parent_ip' é None até receber ACK
                                join_state={
                                    'active':True, 
                                    'stream_id':target, 
                                    'target_ip':nh, 
                                    'last_sent':time.time(), 
                                    'retries':0, 
                                    'parent_ip':None
                                }
                            else:
                                # Mostrar streams disponíveis
                                if node.routing_table:
                                    print(f"[!] Sem rota para '{target}'. Streams disponíveis:")
                                    for sid, e in sorted(node.routing_table.items(), key=lambda x: x[1].custo_acumulado):
                                        print(f"    - {sid}: custo {e.custo_acumulado:.2f} via {e.proximo_salto_ip}")
                                else:
                                    print("[!] Nenhum stream disponível. Aguarde FLOODs...")
                        else:
                            # Comando 'join' sem argumento: listar streams disponíveis
                            if node.routing_table:
                                print(f"\n=== STREAMS DISPONÍVEIS ===")
                                for sid, e in sorted(node.routing_table.items(), key=lambda x: x[1].custo_acumulado):
                                    print(f"  {sid}: custo {e.custo_acumulado:.2f} via {e.proximo_salto_ip}")
                                print("\nUso: join <stream_id>")
                            else:
                                print("[!] Nenhum stream disponível. Aguarde FLOODs...")

    except KeyboardInterrupt: print("\nBye.")
    finally:
        if ffmpeg_source: ffmpeg_source.close()
        sock.close()

if __name__ == "__main__": main()