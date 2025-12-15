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
from typing import Dict

from utils import get_interface_ip
from overlay_structs import OverlayNode, MsgType, MAX_PACKET_SIZE, ClientQoSMetrics, RetransmissionBuffer, FECEncoder

DEFAULT_PORT = 50000
BOOTSTRAP_PORT = 6000
MONITOR_PORT = 6001  # Porta UDP do tracker para monitorização
VIDEO_SOURCE = "trailer_the_boys.mp4" 

# --- CONFIGURAÇÃO REDE ---
CHUNK_SIZE = 500  # Otimizado para evitar fragmentação (MTU ~1500)

# --- CLASSE STREAMER COM ABR (ADAPTIVE BITRATE) ---
class FFmpegStreamer:
    def __init__(self, filename, quality='HIGH'):
        self.filename = filename
        self.current_quality = quality
        print(f"\n{'='*70}")
        print(f"[STREAMER-INIT] Qualidade: {quality}")
        if quality == 'HIGH':
            print(f"[STREAMER-INIT] Bitrate: 400kbps | Resolucao: 640x480 | PREMIUM")
        else:
            print(f"[STREAMER-INIT] Bitrate: 100kbps | Resolucao: 320x240 | OTIMO (anti-perda)")
        print(f"{'='*70}")
        
        # --- PERFIS DE QUALIDADE PREMIUM ---
        if quality == 'HIGH':
            # HIGH: 640x480, 400k video - QUALIDADE EXCELENTE
            scale = "640:480"
            v_bitrate = "400k"   # Qualidade premium sem pixelização
            a_bitrate = "64k"   # Áudio bom
            bufsize = "800k"
            gop = "12"
        else: # LOW
            # LOW: 320x240, 100k video - Otimizado contra quebras com 10% perdas
            scale = "320:240"
            v_bitrate = "100k"   # Bitrate reduzido para evitar artefatos em perdas
            a_bitrate = "32k"    # Áudio reduzido
            bufsize = "200k"     # Buffer menor
            gop = "24"           # GOP maior = menos keyframes = mais robusto

        command = [
            'ffmpeg',
            '-re',
            '-stream_loop', '-1',
            '-i', filename,
            '-vf', f'scale={scale}',
            '-f', 'mpegts',
            '-c:v', 'mpeg2video',
            '-b:v', v_bitrate,
            '-maxrate', v_bitrate,  # Limitar picos
            '-bufsize', bufsize,     # Buffer encoder
            '-qmin', '2',           # Qualidade mínima (menos pixelização)
            '-qmax', '10',          # Qualidade máxima
            '-g', gop,             # GOP 12 frames
            '-bf', '2',             # B-frames para compressão eficiente
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
            
            # --- Tolerância MÁXIMA a Falhas ---
            '-fflags', '+genpts+igndts+discardcorrupt',  # Gerar PTS, ignorar DTS, descartar corrompidos
            '-err_detect', 'ignore_err',     # Ignorar erros e tentar continuar
            
            # --- Sincronização CORRETA (audio = prioridade ao áudio) ---
            '-sync', 'audio',         # Áudio como mestre (sem quebras)
            '-autoexit',              # Fechar automaticamente no fim
            
            # --- Buffer GRANDE para suavidade ---
            '-infbuf',                  # Buffer infinito
            '-framedrop',               # Dropar frames se necessário
            
            # --- Análise RÁPIDA para startup rápido ---
            '-probesize', '32000',      # Análise mínima (startup rápido)
            '-analyzeduration', '500000', # 0.5s análise (foi 2s)
            
            '-window_title', 'Streamer 1',
            '-x', '640', '-y', '480',
            '-loglevel', 'fatal',       # Apenas erros fatais (menos ruído)
            '-'
        ]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

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

    # --- SETUP INICIAL ---
    my_ip = get_interface_ip()
    print(f"[*] Nó {args.node_id} ({my_ip}) ONLINE")

    # Obter vizinhos
    initial_neighbors = get_neighbors_dynamic(args.tracker, args.node_id, my_ip)
    
    node = OverlayNode(args.node_id, my_ip, DEFAULT_PORT)
    for neighbor_ip in initial_neighbors:
        node.neighbors[neighbor_ip] = {'metric': 50.0, 'last_seen': 0}
    
    # --- ATIVAR CRIPTOGRAFIA (Opcional) ---
    # Chave pode vir de variável ambiente ou usar padrão
    # Para desativar: export ENABLE_CRYPTO=0
    enable_crypto = os.environ.get('ENABLE_CRYPTO', '1') == '1'
    if enable_crypto:
        stream_key = os.environ.get('STREAM_KEY', 'overlay_default_key_2025')
        node.security.enable(stream_key)
        print(f"[*] Modo seguro: Conteúdo de vídeo será cifrado AES-256-GCM")
    else:
        print(f"[*] Modo INSEGURO: Criptografia desativada (apenas para testes)")

    # Configurar Socket UDP (Buffer ENORME para absorver perdas)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 10 * 1024 * 1024)  # 10MB
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 10 * 1024 * 1024)  # 10MB
    except: pass
    sock.bind(('0.0.0.0', DEFAULT_PORT))
    sock.setblocking(0)

    # --- INICIALIZAÇÃO DOS COMPONENTES ---
    ffmpeg_source = None
    ffplay_sink = None

    if "STREAMER" in args.node_id:
        if os.path.exists(VIDEO_SOURCE):
            # Inicia em Alta Qualidade por defeito
            ffmpeg_source = FFmpegStreamer(VIDEO_SOURCE, quality='HIGH')
        else:
            print(f"[ERRO] Vídeo '{VIDEO_SOURCE}' não encontrado!")

    if "C" in args.node_id:
        ffplay_sink = FFplayPlayer()

    # Variáveis de Estado
    join_state = {'active': False, 'stream_id': None, 'target_ip': None, 'last_sent': 0, 'retries': 0}
    
    last_hello = 0
    last_flood = 0
    last_report = 0        # <--- FALTAVA ISTO NO TEU SNIPPET
    last_monitor_update = 0  # Para enviar updates ao tracker
    
    # --- SISTEMA DE RETRANSMISSÃO ---
    retx_buffer = RetransmissionBuffer()  # Buffer de pacotes enviados
    client_metrics: Dict[str, ClientQoSMetrics] = {}  # Métricas por cliente
    
    # FEC: k=4 com redundância adaptativa
    # Sistema ADAPTATIVO: ajusta baseado em perdas reais
    # 0-2% perdas: sem redundância, buffer 200ms (RÁPIDO)
    # 2-5% perdas: 1x FEC, buffer 400ms (BALANCEADO)
    # 5-10% perdas: 2x FEC, buffer 600ms (ROBUSTO)
    fec_block_buffer = []  # Acumula k pacotes antes de enviar FEC
    fec_k = 4  # 4 pacotes + 1 paridade = 25% overhead
    fec_redundancy = 1  # Quantas vezes enviar FEC (adaptativo: 1 ou 2)
    
    # Cliente: Controle de recepção COM jitter buffer ADAPTATIVO
    expected_seq = None  # Inicializar com primeiro pacote recebido
    first_packet_received = False
    last_ack_sent = 0
    received_seqs = set()  # Para detetar gaps
    last_nack_time = 0.0
    fec_recovered_count = 0  # Estatística de pacotes recuperados via FEC
    last_cleanup_seq = 0  # Para limpar received_seqs periodicamente
    
    # JITTER BUFFER ADAPTATIVO: ajusta baseado em perdas
    jitter_buffer = {}  # {seq: (data, timestamp)}
    jitter_buffer_delay = 0.3  # Começa em 300ms (estável com 0% perdas)
    jitter_buffer_min_packets = 15  # 15 pacotes = 300ms inicial
    playback_started = False
    
    # Métricas para adaptação (cliente)
    recent_loss_rate = 0.0  # Taxa de perda recente
    loss_history = []  # Histórico de perdas
    last_adaptation = 0.0  # Última vez que ajustamos parâmetros
    
    HELLO_INTERVAL = 1.0 
    FLOOD_INTERVAL = 10.0 
    JOIN_TIMEOUT = 5.0
    MONITOR_UPDATE_INTERVAL = 2.0  # Enviar estado ao tracker a cada 2s

    inputs = [sock, sys.stdin]
    frame_seq = 0
    stats_frames_received = 0
    stats_frames_lost = 0  # Perda FINAL (após FEC+NACK)
    stats_network_lost = 0  # Perda REAL da rede (antes FEC) - NOVO
    
    # Pacing adaptativo: 3ms (estável) inicial, ajusta baseado em feedback
    base_pacing = 0.003  # 3ms base (333 pkt/s - estável sem quebras)
    current_pacing = base_pacing
    packets_sent_burst = 0
    last_burst_reset = time.time()
    last_pacing_adjust = 0.0
    fec_redundancy = 1  # Quantas vezes enviar FEC (1x ou 2x - adaptativo)

    print("[*] Sistema pronto. Comandos: 'join <STREAM_ID>', 'leave <STREAM_ID>', 'status'.")
    
    try:
        while True:
            now = time.time()
            
            # --- 1. Retransmissão de JOIN ---
            if join_state['active']:
                if now - join_state['last_sent'] > JOIN_TIMEOUT:
                    if join_state['retries'] < 3: 
                        print(f"[TIMEOUT] Reenviando JOIN para {join_state['target_ip']}...")
                        pl = json.dumps({"stream_id": join_state['stream_id']}).encode('utf-8')
                        pk = node.pack_message(MsgType.STREAM_JOIN, join_state['target_ip'], pl, encrypt=True)
                        sock.sendto(pk, (join_state['target_ip'], DEFAULT_PORT))
                        join_state['last_sent'] = now
                        join_state['retries'] += 1
                    else:
                        print(f"[ERRO] Falha no JOIN: Vizinho nao responde.")
                        join_state['active'] = False

            # --- 2. Hellos ---
            if now - last_hello >= HELLO_INTERVAL:
                for n_ip in node.neighbors:
                    pkt = node.pack_message(MsgType.HELLO, n_ip, b"", encrypt=True)
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
                    pkt = node.pack_message(MsgType.ROUTE_DISCOVERY, n_ip, flood_payload, encrypt=True)
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
                    # PERDA REAL DA REDE (antes de FEC/NACK) - CRÍTICO para ABR!
                    total_network = stats_frames_received + stats_network_lost
                    network_loss_rate = (stats_network_lost / total_network * 100.0) if total_network > 0 else 0.0
                    
                    # Perda final (após recuperação) - só para debug
                    total_final = stats_frames_received + stats_frames_lost
                    final_loss_rate = (stats_frames_lost / total_final * 100.0) if total_final > 0 else 0.0
                    
                    # ADAPTAÇÃO DINÂMICA DO JITTER BUFFER (baseado em perdas)
                    loss_history.append(network_loss_rate)
                    if len(loss_history) > 5:  # Manter últimas 5 medições
                        loss_history.pop(0)
                    recent_loss_rate = sum(loss_history) / len(loss_history)
                    
                    # Ajustar jitter buffer a cada 2 segundos (resposta mais rápida)
                    if now - last_adaptation >= 2.0:
                        old_delay = jitter_buffer_delay
                        old_packets = jitter_buffer_min_packets
                        
                        # Limiares mais conservadores para evitar oscilações
                        if recent_loss_rate < 0.5:  # <0.5% perdas = MODO RÁPIDO
                            jitter_buffer_delay = 0.3  # 300ms (não 200ms - mais estável)
                            jitter_buffer_min_packets = 15
                        elif recent_loss_rate < 2.0:  # 0.5-2% = MODO BALANCEADO
                            jitter_buffer_delay = 0.4  # 400ms
                            jitter_buffer_min_packets = 20
                        elif recent_loss_rate < 5.0:  # 2-5% = MODO DEFENSIVO
                            jitter_buffer_delay = 0.5  # 500ms
                            jitter_buffer_min_packets = 25
                        else:  # >5% = MODO ROBUSTO
                            jitter_buffer_delay = 0.65  # 650ms (mais margem)
                            jitter_buffer_min_packets = 33
                        
                        if old_delay != jitter_buffer_delay:
                            print(f"\n[ADAPTAÇÃO] Perdas: {recent_loss_rate:.1f}% | Buffer: {old_delay*1000:.0f}ms -> {jitter_buffer_delay*1000:.0f}ms")
                        
                        last_adaptation = now
                    
                    # DICA DE TESTE: Para testar a troca de qualidade, podes descomentar:
                    # network_loss_rate = 15.0 

                    if join_state['stream_id'] and join_state['target_ip']:
                         report_payload = json.dumps({
                             "stream_id": join_state['stream_id'], 
                             "client_id": args.node_id, 
                             "loss_rate": network_loss_rate,  # <-- PERDA REAL!
                             "final_loss_rate": final_loss_rate,  # Para debug
                             "fec_recovered": fec_recovered_count
                         }).encode('utf-8')
                         
                         # LOG: Envio de relatório
                         print(f"\n[CLIENTE-QoS] ENVIANDO Relatorio: Rede={network_loss_rate:.1f}% Final={final_loss_rate:.1f}% FEC={fec_recovered_count}")
                         print(f"[CLIENTE-QoS] Destino: {join_state['target_ip']} Stream: {join_state['stream_id']}")
                         
                         pkt = node.pack_message(MsgType.STREAM_REPORT, join_state['target_ip'], report_payload, encrypt=True)
                         sock.sendto(pkt, (join_state['target_ip'], DEFAULT_PORT))
                         
                         stats_frames_received = 0
                         stats_frames_lost = 0
                         stats_network_lost = 0  # Reset perda real também
                         fec_recovered_count = 0  # Reset contador FEC
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
                                "data": b64_data,
                                "timestamp": now
                            }).encode('utf-8')
                            
                            # Guardar no buffer de retransmissão (CIFRADO)
                            pkt = node.pack_message(MsgType.STREAM_DATA, "broadcast", payload, encrypt=True)
                            retx_buffer.add(frame_seq, pkt)
                            
                            # FEC: Adicionar DADOS RAW ao bloco (não o payload JSON inteiro)
                            fec_block_buffer.append(raw_chunk)

                            # Enviar pacotes de dados
                            for client_ip in clients:
                                # Inicializar métricas se necessário
                                if client_ip not in client_metrics:
                                    client_metrics[client_ip] = ClientQoSMetrics(client_ip=client_ip)
                                
                                sock.sendto(pkt, (client_ip, DEFAULT_PORT))
                            
                            # FEC: Quando tivermos k pacotes, enviar paridade
                            if len(fec_block_buffer) >= fec_k:
                                # Gerar pacote de paridade dos DADOS RAW
                                parity_data = FECEncoder.generate_parity(fec_block_buffer[:fec_k])
                                block_id = (frame_seq - 1) // fec_k
                                
                                # Incluir tamanhos dos pacotes originais para recuperação correta
                                packet_sizes = [len(p) for p in fec_block_buffer[:fec_k]]
                                
                                fec_payload = json.dumps({
                                    "id": args.node_id,
                                    "block_id": block_id,
                                    "k": fec_k,
                                    "parity": base64.b64encode(parity_data).decode('utf-8'),
                                    "sizes": packet_sizes  # Para trim correto na recuperação
                                }).encode('utf-8')
                                
                                fec_pkt = node.pack_message(MsgType.STREAM_FEC, "broadcast", fec_payload, encrypt=True)
                                
                                # LOG: Envio de FEC
                                if frame_seq % 50 == 0:  # A cada 50 frames
                                    print(f"\n[STREAMER-FEC] Block {block_id}: {len(packet_sizes)} pacotes - Redundância: {fec_redundancy}x")
                                
                                # ESTRATÉGIA ADAPTATIVA: FEC com redundância baseada em feedback
                                # fec_redundancy é ajustado pelo relatório dos clientes (1x ou 2x)
                                for client_ip in clients:
                                    for _ in range(fec_redundancy):
                                        sock.sendto(fec_pkt, (client_ip, DEFAULT_PORT))
                                        if fec_redundancy > 1:
                                            time.sleep(0.001)  # 1ms entre duplicatas
                                
                                # Limpar buffer FEC
                                fec_block_buffer = fec_block_buffer[fec_k:]
                            
                            # NOVA ESTRATÉGIA: Taxa constante em vez de bursts
                            time.sleep(current_pacing)
                            
                            # LOG: Estatísticas periódicas
                            if frame_seq % 200 == 0:
                                num_clients = len(clients)
                                total_sent = frame_seq
                                fec_sent = frame_seq // fec_k
                                print(f"\n[STREAMER-STATS] Frames: {total_sent} | FEC: {fec_sent} | Clientes: {num_clients}")
                                print(f"[STREAMER-STATS] Cifrados: {node.stats_encrypted_sent} pacotes")
                                if client_metrics:
                                    for cip, cm in client_metrics.items():
                                        print(f"[STREAMER-STATS]   {cip}: Loss {cm.loss_rate:.1f}%")
                            
                            # ADAPTAÇÃO A CADA 2 SEGUNDOS baseado em feedback dos clientes
                            packets_sent_burst += 1
                            if now - last_pacing_adjust > 2.0:
                                if client_metrics:
                                    max_loss = max((m.loss_rate for m in client_metrics.values()), default=0)
                                    old_pacing = current_pacing
                                    old_redundancy = fec_redundancy
                                    
                                    # Ajustar pacing e redundância FEC (mais conservador)
                                    if max_loss < 0.5:  # <0.5% perdas = MODO RÁPIDO
                                        current_pacing = 0.003  # 3ms (333 pkt/s - estável)
                                        fec_redundancy = 1  # Sem redundância
                                    elif max_loss < 3.0:  # 0.5-3% = MODO NORMAL
                                        current_pacing = 0.003  # 3ms (mantém estável)
                                        fec_redundancy = 1  # Sem redundância
                                    elif max_loss < 6.0:  # 3-6% = MODO DEFENSIVO
                                        current_pacing = 0.004  # 4ms (250 pkt/s)
                                        fec_redundancy = 2  # 2x redundância
                                    else:  # >6% = MODO ROBUSTO
                                        current_pacing = 0.005  # 5ms (200 pkt/s)
                                        fec_redundancy = 2  # 2x redundância
                                    
                                    if old_pacing != current_pacing or old_redundancy != fec_redundancy:
                                        print(f"\n[STREAMER-ADAPT] Perdas: {max_loss:.1f}% | Pacing: {old_pacing*1000:.0f}ms->{current_pacing*1000:.0f}ms | FEC: {old_redundancy}x->{fec_redundancy}x")
                                
                                packets_sent_burst = 0
                                last_pacing_adjust = now
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
                        header, payload = node.unpack_message(data)
                        if not header: continue

                        # --- PROCESSAMENTO ---
                        if header['type'] == MsgType.HELLO:
                            resp = node.handle_hello(header, sender_ip_real)
                            pkt = node.pack_message(MsgType.HELLO_RESPONSE, sender_ip_real, resp, encrypt=True)
                            sock.sendto(pkt, (sender_ip_real, DEFAULT_PORT))

                        elif header['type'] == MsgType.HELLO_RESPONSE:
                            node.handle_hello_response(payload, sender_ip_real)

                        elif header['type'] == MsgType.ROUTE_DISCOVERY:
                            new_payload = node.handle_flood(header, payload, sender_ip_real)
                            if new_payload:
                                for n_ip in node.neighbors:
                                    if n_ip != sender_ip_real:
                                        pkt = node.pack_message(MsgType.ROUTE_DISCOVERY, n_ip, new_payload, encrypt=True)
                                        sock.sendto(pkt, (n_ip, DEFAULT_PORT))

                        elif header['type'] == MsgType.STREAM_JOIN:
                            upstream_ip, send_ack = node.handle_join(payload, sender_ip_real)
                            if send_ack:
                                try:
                                    jdata = json.loads(payload.decode('utf-8'))
                                    sid = jdata['stream_id']
                                    ack_pl = json.dumps({"stream_id": sid}).encode('utf-8')
                                    ack_pkt = node.pack_message(MsgType.ACK_JOIN, sender_ip_real, ack_pl, encrypt=True)
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
                                    pkt = node.pack_message(MsgType.STREAM_JOIN, upstream_ip, payload, encrypt=True)
                                    sock.sendto(pkt, (upstream_ip, DEFAULT_PORT))

                        elif header['type'] == MsgType.ACK_JOIN:
                            if join_state['active']:
                                print(f"[OK] ACK recebido! Ligacao OK.")
                                join_state['active'] = False 

                        elif header['type'] == MsgType.STREAM_LEAVE:
                            upstream_prune = node.handle_leave(payload, sender_ip_real)
                            if upstream_prune and upstream_prune != "SOURCE":
                                pkt = node.pack_message(MsgType.STREAM_LEAVE, upstream_prune, payload, encrypt=True)
                                sock.sendto(pkt, (upstream_prune, DEFAULT_PORT))

                        # --- ACK/NACK HANDLING ---
                        elif header['type'] == MsgType.STREAM_ACK:
                            # Atualizar métricas do cliente
                            try:
                                ack_info = json.loads(payload.decode('utf-8'))
                                client_id = ack_info.get('client_id')
                                last_seq = ack_info.get('last_seq', 0)
                                fec_recovered = ack_info.get('fec_recovered', 0)
                                
                                if sender_ip_real in client_metrics:
                                    metrics = client_metrics[sender_ip_real]
                                    metrics.last_ack_seq = last_seq
                                    metrics.packets_received = ack_info.get('packets_received', 0)
                                    metrics.packets_lost = ack_info.get('packets_lost', 0)
                                    metrics.update_loss_rate()
                                    
                                    # Log de FEC effectiveness
                                    if fec_recovered > 0 and (stats_frames_received + stats_frames_lost) % 200 == 0:
                                        print(f"\n[FEC] Cliente {sender_ip_real}: FEC recuperou {fec_recovered} pacotes")
                            except: pass
                        
                        elif header['type'] == MsgType.STREAM_NACK:
                            # Retransmitir pacotes perdidos
                            try:
                                nack_info = json.loads(payload.decode('utf-8'))
                                missing_seqs = nack_info.get('missing_seqs', [])
                                stream_id = nack_info.get('stream_id')
                                client_id = nack_info.get('client_id')
                                
                                if args.node_id == stream_id:
                                    # Sou o streamer - retransmitir
                                    retx_count = 0
                                    for seq in missing_seqs:
                                        retx_pkt = retx_buffer.get(seq)
                                        if retx_pkt:
                                            # Modificar header para STREAM_RETX em vez de recriar pacote
                                            # Extrai payload original e recria como STREAM_RETX (CIFRADO)
                                            _, original_payload = node.unpack_message(retx_pkt)
                                            if original_payload:
                                                retx_packet = node.pack_message(MsgType.STREAM_RETX, sender_ip_real, original_payload, encrypt=True)
                                                sock.sendto(retx_packet, (sender_ip_real, DEFAULT_PORT))
                                                retx_count += 1
                                    
                                    if retx_count > 0:
                                        print(f"\n[RTX] Retransmitidos {retx_count} pacotes para {sender_ip_real}")
                                        
                                        # Atualizar métricas
                                        if sender_ip_real in client_metrics:
                                            metrics = client_metrics[sender_ip_real]
                                            metrics.consecutive_losses = len(missing_seqs)
                                
                                else:
                                    # Sou router - encaminhar NACK upstream
                                    if stream_id in node.routing_table:
                                        upstream = node.routing_table[stream_id].proximo_salto_ip
                                        nack_pkt = node.pack_message(MsgType.STREAM_NACK, upstream, payload, encrypt=True)
                                        sock.sendto(nack_pkt, (upstream, DEFAULT_PORT))
                            except Exception as e:
                                print(f"Erro NACK: {e}")

                        # --- ADAPTIVE BITRATE LOGIC ---
                        elif header['type'] == MsgType.STREAM_REPORT:
                            # 1. Router: Encaminhar
                            upstream_report = node.handle_report(payload, sender_ip_real)
                            if upstream_report and upstream_report != "SOURCE":
                                pkt = node.pack_message(MsgType.STREAM_REPORT, upstream_report, payload, encrypt=True)
                                sock.sendto(pkt, (upstream_report, DEFAULT_PORT))
                            
                            # 2. Streamer: Decidir Qualidade (ABR Inteligente)
                            elif upstream_report == "SOURCE" and ffmpeg_source:
                                try:
                                    info = json.loads(payload.decode('utf-8'))
                                    loss = info.get('loss_rate', 0.0)  # Perda REAL da rede
                                    final_loss = info.get('final_loss_rate', 0.0)  # Perda ap\u00f3s FEC
                                    fec_recovered = info.get('fec_recovered', 0)
                                    client_id = info.get('client_id', 'unknown')
                                    
                                    # Log detalhado para debug
                                    print(f"\n[STREAMER] >> Relatorio C6: Perda {loss:.1f}%")
                                    print(f"[STREAMER] >> (Limiar ABR: 8% HIGH->LOW)")
                                    
                                    # Atualizar metricas do cliente
                                    if sender_ip_real in client_metrics:
                                        metrics = client_metrics[sender_ip_real]
                                        metrics.loss_rate = loss  # Usar perda REAL da rede
                                        metrics.last_report_time = now
                                    
                                    # Calcular média de perda de todos os clientes
                                    if client_metrics:
                                        avg_loss = sum(m.loss_rate for m in client_metrics.values()) / len(client_metrics)
                                        max_loss = max(m.loss_rate for m in client_metrics.values())
                                        
                                        # Estratégia: Bitrate baixo permite HIGH até 5%
                                        # > 5% perda OU > 4 perdas consecutivas = LOW (100kbps)
                                        # < 2% perda = HIGH (400kbps)
                                        worst_consecutive = max((m.consecutive_losses for m in client_metrics.values()), default=0)
                                        
                                        # LOG: Avaliação ABR (sempre mostrar)
                                        print(f"[STREAMER-ABR] Qualidade atual: {ffmpeg_source.current_quality}")
                                        print(f"[STREAMER-ABR] Max Loss: {max_loss:.1f}% | Avg: {avg_loss:.1f}% | Consecutivas: {worst_consecutive}")
                                        print(f"[STREAMER-ABR] Limiar HIGH->LOW: 5% | LOW->HIGH: 2%")
                                        
                                        if (max_loss > 5.0 or worst_consecutive >= 4) and ffmpeg_source.current_quality == 'HIGH':
                                            print(f"\n" + "="*70)
                                            print(f"[ABR-MUDANCA] HIGH -> LOW")
                                            print(f"[ABR-MUDANCA] Motivo: Perda {max_loss:.1f}% (limiar: 5%)")
                                            print(f"[ABR-MUDANCA] Bitrate: 400kbps -> 100kbps")
                                            print(f"[ABR-MUDANCA] Resolucao: 640x480 -> 320x240")
                                            print(f"[ABR-MUDANCA] Consecutivas: {worst_consecutive} | Media: {avg_loss:.1f}%")
                                            print("="*70)
                                            ffmpeg_source.close()
                                            ffmpeg_source = FFmpegStreamer(VIDEO_SOURCE, quality='LOW')
                                            for m in client_metrics.values():
                                                m.consecutive_losses = 0
                                                m.current_quality = 'LOW'
                                        
                                        elif max_loss < 2.0 and worst_consecutive == 0 and ffmpeg_source.current_quality == 'LOW':
                                            print(f"\n" + "="*70)
                                            print(f"[ABR-MUDANCA] LOW -> HIGH")
                                            print(f"[ABR-MUDANCA] Motivo: Rede estavel ({max_loss:.1f}% < 2%)")
                                            print(f"[ABR-MUDANCA] Bitrate: 100kbps -> 400kbps")
                                            print(f"[ABR-MUDANCA] Resolucao: 320x240 -> 640x480")
                                            print(f"[ABR-MUDANCA] Media: {avg_loss:.1f}%")
                                            print("="*70)
                                            ffmpeg_source.close()
                                            ffmpeg_source = FFmpegStreamer(VIDEO_SOURCE, quality='HIGH')
                                            for m in client_metrics.values():
                                                m.current_quality = 'HIGH'
                                    else:
                                        # Fallback: lógica antiga
                                        if loss > 8.0 and ffmpeg_source.current_quality == 'HIGH':
                                            print(f"\n[!!] CONGESTIONAMENTO (Perda: {loss:.1f}%) -> LOW PROFILE")
                                            ffmpeg_source.close()
                                            ffmpeg_source = FFmpegStreamer(VIDEO_SOURCE, quality='LOW')
                                        
                                        elif loss < 2.0 and ffmpeg_source.current_quality == 'LOW':
                                            print(f"\n[OK] REDE RECUPERADA (Perda: {loss:.1f}%) -> HIGH PROFILE")
                                            ffmpeg_source.close()
                                            ffmpeg_source = FFmpegStreamer(VIDEO_SOURCE, quality='HIGH')
                                except Exception as e: 
                                    pass  # Silenciar erros ABR para evitar UTF-8 issues

                        # --- PACOTES FEC ---
                        elif header['type'] == MsgType.STREAM_FEC:
                            try:
                                fec_info = json.loads(payload.decode('utf-8'))
                                s_id = fec_info.get('id')
                                block_id = fec_info.get('block_id')
                                parity_b64 = fec_info.get('parity')
                                sizes = fec_info.get('sizes', [])  # Tamanhos dos pacotes
                                
                                # Router: Forwarding
                                if s_id in node.routing_table:
                                    for child in node.routing_table[s_id].downstream_ips:
                                        if child != sender_ip_real:
                                            pkt = node.pack_message(MsgType.STREAM_FEC, child, payload, encrypt=True)
                                            sock.sendto(pkt, (child, DEFAULT_PORT))
                                
                                # Cliente: Armazenar FEC para possível recuperação (filtrar duplicatas)
                                if ffplay_sink and parity_b64:
                                    parity_data = base64.b64decode(parity_b64)
                                    
                                    # Verificar se é duplicata (já temos este bloco)
                                    is_duplicate = (block_id in node.fec_decoder.blocks and 
                                                   node.fec_decoder.blocks[block_id].parity_packet is not None)
                                    
                                    # LOG: Recepção FEC
                                    if block_id % 25 == 0:  # A cada 25 blocos
                                        dup_marker = " [DUPLICATA]" if is_duplicate else ""
                                        print(f"\n[CLIENTE-FEC] Block {block_id}: {len(sizes)} pacotes, parity {len(parity_data)} bytes{dup_marker}")
                                    
                                    # add_fec_packet já previne duplicação internamente
                                    node.fec_decoder.add_fec_packet(block_id, parity_data, sizes)
                                    
                            except Exception as e:
                                pass

                        # --- VÍDEO DATA ---
                        elif header['type'] == MsgType.STREAM_DATA or header['type'] == MsgType.STREAM_RETX:
                            try:
                                info = json.loads(payload.decode('utf-8'))
                                s_id = info.get('id')
                                recv_seq = info.get('seq', 0)
                                
                                # Router: Forwarding
                                if s_id in node.routing_table:
                                    for child in node.routing_table[s_id].downstream_ips:
                                        if child != sender_ip_real:
                                            msg_type = MsgType.STREAM_RETX if header['type'] == MsgType.STREAM_RETX else MsgType.STREAM_DATA
                                            pkt = node.pack_message(msg_type, child, payload, encrypt=True)
                                            sock.sendto(pkt, (child, DEFAULT_PORT))
                                
                                # Cliente: Play + Detecção de Perdas com FEC IMEDIATO
                                if ffplay_sink:
                                    b64_data = info.get('data')
                                    raw_data = base64.b64decode(b64_data)
                                    packet_timestamp = info.get('timestamp', now)
                                    
                                    # Inicializar expected_seq com primeiro pacote
                                    if expected_seq is None:
                                        expected_seq = recv_seq
                                        first_packet_received = True
                                        playback_started = False  # Aguardar buffer
                                        print(f"\n{'='*70}")
                                        print(f"[CLIENTE-INIT] Primeiro pacote recebido: seq={recv_seq}")
                                        print(f"[CLIENTE-INIT] Buffer ADAPTATIVO: inicia em {jitter_buffer_delay*1000:.0f}ms")
                                        print(f"[CLIENTE-INIT] FEC: k=4 (25% overhead) + NACK 30ms")
                                        print(f"[CLIENTE-INIT] Adaptará buffer automaticamente (200-600ms)")
                                        print(f"[CLIENTE-INIT] Criptografia AES-256-GCM: ATIVA")
                                        print(f"{'='*70}")
                                    
                                    # Adicionar ao JITTER BUFFER em vez de reproduzir imediatamente
                                    jitter_buffer[recv_seq] = (raw_data, packet_timestamp)
                                    
                                    # Adicionar DADOS RAW ao decoder FEC (não o payload JSON)
                                    node.fec_decoder.add_data_packet(recv_seq, raw_data)
                                    
                                    # DETECÇÃO DE GAP
                                    if recv_seq > expected_seq:
                                        gap_size = recv_seq - expected_seq
                                        
                                        # LOG: Gap detectado
                                        if gap_size > 3:  # Só logar gaps grandes
                                            print(f"\n[CLIENTE-GAP] Gap de {gap_size} pacotes detectado ({expected_seq}->{recv_seq})")
                                        
                                        # CONTAR PERDA REAL DA REDE (ANTES de qualquer recuperação)
                                        stats_network_lost += gap_size  # <-- PERDA REAL
                                        
                                        # ESTRATÉGIA: FEC adiciona ao JITTER BUFFER (mantém sincronia AV)
                                        recovered_count = 0
                                        fec_failed_count = 0
                                        for missing_seq in range(expected_seq, recv_seq):
                                            # Verificar se já não foi recuperado/recebido antes
                                            if missing_seq in received_seqs:
                                                recovered_count += 1
                                                continue
                                                
                                            # Tentar recuperar via FEC
                                            fec_data = node.fec_decoder.get_recovered(missing_seq)
                                            if fec_data:
                                                try:
                                                    # Validação MODERADA: dados não vazios e tamanho razoável
                                                    if (len(fec_data) > 0 and 
                                                        len(fec_data) <= CHUNK_SIZE * 2 and
                                                        len(fec_data) >= 20):  # Mínimo flexível
                                                        # ADICIONAR AO JITTER BUFFER em vez de reproduzir direto
                                                        # Usar timestamp estimado baseado no gap
                                                        estimated_ts = packet_timestamp - (recv_seq - missing_seq) * 0.02
                                                        jitter_buffer[missing_seq] = (fec_data, estimated_ts)
                                                        fec_recovered_count += 1
                                                        recovered_count += 1
                                                        received_seqs.add(missing_seq)
                                                    else:
                                                        fec_failed_count += 1
                                                except Exception as e:
                                                    # Pacote FEC inválido - será tratado por NACK
                                                    fec_failed_count += 1
                                            else:
                                                fec_failed_count += 1
                                        
                                        # Calcular perdas FINAIS (após FEC e verificação de received_seqs)
                                        actual_lost = gap_size - recovered_count
                                        if actual_lost > 0:
                                            stats_frames_lost += actual_lost
                                        
                                        # LOG: Resultado da recuperação FEC
                                        if gap_size > 3:
                                            print(f"[CLIENTE-FEC] Gap {gap_size}: FEC OK={recovered_count} | FEC FAIL={fec_failed_count} | Perdido={actual_lost}")
                                        
                                        # NACK ULTRA AGRESSIVO: Recuperar rapidamente o que FEC não pegou
                                        if actual_lost > 0 and now - last_nack_time > 0.05:  # 50ms cooldown
                                            missing_seqs = []
                                            for seq in range(expected_seq, recv_seq):
                                                # Verificar se realmente está perdido (não em received_seqs)
                                                if seq not in received_seqs:
                                                    missing_seqs.append(seq)
                                            
                                            if missing_seqs and join_state['target_ip']:
                                                nack_payload = json.dumps({
                                                    "stream_id": s_id,
                                                    "missing_seqs": missing_seqs[:10],  # Máximo 10
                                                    "client_id": args.node_id
                                                }).encode('utf-8')
                                                nack_pkt = node.pack_message(MsgType.STREAM_NACK, join_state['target_ip'], nack_payload, encrypt=True)
                                                sock.sendto(nack_pkt, (join_state['target_ip'], DEFAULT_PORT))
                                                last_nack_time = now
                                                
                                                if recovered_count > 0:
                                                    print(f"\n[FEC] Recuperados: {recovered_count} | NACK: {len(missing_seqs)} (Gap: {gap_size})")
                                        
                                        # Atualizar expected_seq para depois do gap
                                        expected_seq = recv_seq
                                    
                                    # REPRODUZIR DO JITTER BUFFER (adaptativo)
                                    if not playback_started and len(jitter_buffer) >= jitter_buffer_min_packets:
                                        playback_started = True
                                        print(f"\n{'='*70}")
                                        print(f"[CLIENTE-BUFFER] Iniciando playback")
                                        print(f"[CLIENTE-BUFFER] Buffer acumulado: {len(jitter_buffer)} pacotes ({jitter_buffer_delay*1000:.0f}ms)")
                                        print(f"[CLIENTE-BUFFER] Sistema ADAPTATIVO ativado")
                                        print(f"{'='*70}")
                                    
                                    if playback_started:
                                        # Reproduzir pacotes que já passaram do delay
                                        seqs_to_play = []
                                        for seq, (data, ts) in jitter_buffer.items():
                                            if now - ts >= jitter_buffer_delay:
                                                seqs_to_play.append(seq)
                                        
                                        # Reproduzir em ordem
                                        for seq in sorted(seqs_to_play):
                                            if seq not in received_seqs:
                                                data, _ = jitter_buffer[seq]
                                                if len(data) > 0 and len(data) <= CHUNK_SIZE * 1.5:
                                                    ffplay_sink.write_data(data)
                                                    stats_frames_received += 1
                                                    received_seqs.add(seq)
                                            # Remover do buffer
                                            del jitter_buffer[seq]
                                        
                                        # Atualizar expected_seq
                                        if recv_seq >= expected_seq:
                                            expected_seq = recv_seq + 1
                                    
                                    # Cleanup periódico + Stats de criptografia
                                    if stats_frames_received % 100 == 0:
                                        node.fec_decoder.cleanup_old_blocks(recv_seq)
                                        # Limpar received_seqs antigos (manter só últimos 100 seqs)
                                        if recv_seq > last_cleanup_seq + 100:
                                            received_seqs = {s for s in received_seqs if s > recv_seq - 100}
                                            last_cleanup_seq = recv_seq
                                        
                                        # Log de criptografia
                                        if stats_frames_received % 200 == 0:
                                            print(f"\n[CLIENTE-STATS] Recebidos: {stats_frames_received} | Decifrados: {node.stats_encrypted_recv}")
                                            if node.stats_decrypt_failed > 0:
                                                print(f"[CLIENTE-WARN] Falhas autenticação: {node.stats_decrypt_failed}")
                                    
                                    # ACK Cumulativo
                                    if stats_frames_received % 25 == 0 and join_state['target_ip']:
                                        ack_payload = json.dumps({
                                            "stream_id": s_id,
                                            "last_seq": recv_seq,
                                            "client_id": args.node_id,
                                            "packets_received": stats_frames_received,
                                            "packets_lost": stats_frames_lost,
                                            "fec_recovered": fec_recovered_count
                                        }).encode('utf-8')
                                        ack_pkt = node.pack_message(MsgType.STREAM_ACK, join_state['target_ip'], ack_payload, encrypt=True)
                                        sock.sendto(ack_pkt, (join_state['target_ip'], DEFAULT_PORT))
                                    
                                    if stats_frames_received % 100 == 0:
                                        # Mostrar perda REAL da rede e perda FINAL
                                        total_net = stats_frames_received + stats_network_lost
                                        net_loss = (stats_network_lost / total_net * 100.0) if total_net > 0 else 0.0
                                        
                                        total_final = stats_frames_received + stats_frames_lost
                                        final_loss = (stats_frames_lost / total_final * 100.0) if total_final > 0 else 0.0
                                        
                                        fec_rate = (fec_recovered_count / total_net * 100.0) if total_net > 0 else 0.0
                                        fec_effectiveness = ((stats_network_lost - stats_frames_lost) / stats_network_lost * 100.0) if stats_network_lost > 0 else 0.0
                                        
                                        print(f"\r[CLIENTE-STATS] RX: {stats_frames_received} | Rede: {net_loss:.1f}% | Final: {final_loss:.1f}% | FEC: {fec_rate:.1f}% (Eficacia: {fec_effectiveness:.0f}%)", end="")

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
                                pk = node.pack_message(MsgType.STREAM_JOIN, nh, pl, encrypt=True)
                                sock.sendto(pk, (nh, DEFAULT_PORT))
                                join_state['active'] = True
                                join_state['stream_id'] = target
                                join_state['target_ip'] = nh
                                join_state['last_sent'] = time.time()
                                print(f"[JOIN] Pedido JOIN enviado para {nh}")
                            else: print("[!] Sem rota. Aguarde flood.")

    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        if ffmpeg_source: ffmpeg_source.close()
        sock.close()

if __name__ == "__main__":
    main()