import socket
import sys
import select
import json
import argparse
import time
import base64
import math
import threading
import os
from utils import get_interface_ip
from overlay_structs import OverlayNode, MsgType, MAX_PACKET_SIZE

# Tentar importar Tkinter para visualização (Pode falhar em headless)
try:
    import tkinter as tk
    from tkinter import Label
    HAS_GUI = True
except ImportError:
    HAS_GUI = False

DEFAULT_PORT = 50000
VIDEO_FILE = "movie.Mjpeg"  # O ficheiro que o prof recomendou

# --- CLASSE PARA LER O VÍDEO DO DISCO ---
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
        """Lê o próximo frame JPEG do ficheiro MJPEG (formato específico)"""
        if not self.file: return None
        try:
            # Ler o tamanho do frame (5 bytes string numérico)
            data = self.file.read(5)
            if not data: 
                self.file.seek(0) # Loop video
                data = self.file.read(5)
            
            framelength = int(data)
            data = self.file.read(framelength)
            self.frame_num += 1
            return data
        except: return None

# --- CLASSE PARA MOSTRAR O VÍDEO (GUI) ---
class VideoGUI:
    def __init__(self, root, title):
        self.root = root
        self.root.title(title)
        self.label = Label(root)
        self.label.pack()
        self.root.update()

    def update_image(self, data_bytes):
        try:
            # Tkinter suporta GIF/PPM nativamente. Para JPEG precisa de PIL.
            # Tente instalar 'python3-pil.imagetk' se der erro.
            # Aqui usamos uma conversão básica ou raw data se suportado.
            image = tk.PhotoImage(data=data_bytes) 
            self.label.configure(image=image)
            self.label.image = image
            self.root.update()
        except Exception as e:
            # Fallback para debug se não tiver PIL instalado para JPEGs
            pass

def load_config(file_path, node_id):
    try:
        with open(file_path, 'r') as f:
            config = json.load(f)
        for node in config.get("nodes", []):
            if node["id"] == node_id: return node
        return None
    except: return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('node_id')
    parser.add_argument('--config', default='bootstrap_conf.json') 
    args = parser.parse_args()

    my_ip = get_interface_ip()
    print(f"[*] Nó {args.node_id} ({my_ip}) a iniciar...")

    node_config = load_config(args.config, args.node_id)
    node = OverlayNode(args.node_id, my_ip, DEFAULT_PORT)
    
    if node_config:
        for neighbor_ip in node_config.get("neighbors", []):
            node.neighbors[neighbor_ip] = {'metric': 50.0, 'last_seen': 0}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', DEFAULT_PORT))
    sock.setblocking(0)

    # --- SETUP VÍDEO ---
    video_stream = None
    gui = None
    
    if "STREAMER" in args.node_id:
        video_stream = VideoStream(VIDEO_FILE)
    
    if "C" in args.node_id and HAS_GUI:
        try:
            root = tk.Tk()
            gui = VideoGUI(root, f"Cliente {args.node_id}")
            print("[GUI] Janela de vídeo iniciada.")
        except:
            print("[GUI] Falha ao iniciar janela gráfica.")

    # --- ESTADO DE REMONTAGEM (CLIENTE) ---
    # buffer[frame_id] = { chunk_index: data_b64 }
    reassembly_buffer = {} 

    # --- TIMERS ---
    last_hello = 0
    last_flood = 0
    last_frame = 0
    
    HELLO_INTERVAL = 1.0 
    FLOOD_INTERVAL = 10.0 
    FRAME_INTERVAL = 0.05 # 20 FPS (Tentar ser rápido)
    
    # Tamanho seguro para payload (deixar espaço para cabeçalhos JSON e UDP)
    CHUNK_SIZE = 1024 

    inputs = [sock, sys.stdin]
    is_streamer = "STREAMER" in args.node_id
    frame_seq = 0

    print("[*] Loop principal iniciado. Ctrl+C para sair.")
    
    try:
        while True:
            now = time.time()
            
            # --- A. Enviar Hellos ---
            if now - last_hello >= HELLO_INTERVAL:
                for n_ip in node.neighbors:
                    pkt = node.pack_message(MsgType.HELLO, n_ip, b"")
                    node.pending_pings[node.sequence_number] = now
                    sock.sendto(pkt, (n_ip, DEFAULT_PORT))
                last_hello = now

            # --- B. Flood ---
            if is_streamer and now - last_flood >= FLOOD_INTERVAL:
                flood_payload = json.dumps({
                    "stream_id": args.node_id,
                    "cost": 0,
                    "origin_seq": int(now)
                }).encode('utf-8')
                
                print(f"[📢] A iniciar Flood para {args.node_id}")
                for n_ip in node.neighbors:
                    pkt = node.pack_message(MsgType.ROUTE_DISCOVERY, n_ip, flood_payload)
                    sock.sendto(pkt, (n_ip, DEFAULT_PORT))
                last_flood = now

            # --- C. GERAR E FRAGMENTAR VÍDEO (SÓ STREAMER) ---
            if is_streamer and video_stream and now - last_frame >= FRAME_INTERVAL:
                if args.node_id in node.routing_table:
                    clients = node.routing_table[args.node_id].downstream_ips
                    if clients:
                        # 1. Ler bytes do disco
                        raw_bytes = video_stream.next_frame()
                        
                        if raw_bytes:
                            frame_seq += 1
                            # 2. Codificar para Base64 (para ir dentro do JSON)
                            b64_data = base64.b64encode(raw_bytes).decode('utf-8')
                            total_len = len(b64_data)
                            
                            # 3. Calcular fragmentos
                            num_chunks = math.ceil(total_len / CHUNK_SIZE)
                            
                            # 4. Enviar Fragmentos
                            for i in range(num_chunks):
                                start = i * CHUNK_SIZE
                                end = start + CHUNK_SIZE
                                chunk_data = b64_data[start:end]
                                
                                payload = json.dumps({
                                    "id": args.node_id,
                                    "fid": frame_seq,      # ID do Frame
                                    "cid": i,              # ID do Chunk
                                    "tot": num_chunks,     # Total Chunks
                                    "data": chunk_data
                                }).encode('utf-8')

                                for client_ip in clients:
                                    pkt = node.pack_message(MsgType.STREAM_DATA, client_ip, payload)
                                    sock.sendto(pkt, (client_ip, DEFAULT_PORT))
                            
                            # print(f"[🎥] Frame {frame_seq} enviado em {num_chunks} pedaços.")
                last_frame = now

            # --- EVENT LOOP ---
            readable, _, _ = select.select(inputs, [], [], 0.01) # Ultra rápido para vídeo
            
            for s in readable:
                if s is sock:
                    try:
                        data, addr = sock.recvfrom(MAX_PACKET_SIZE)
                        sender_ip_real = addr[0]
                        
                        header, payload = node.unpack_message(data)
                        if not header: continue

                        if header['type'] == MsgType.HELLO:
                            resp_payload = node.handle_hello(header, sender_ip_real)
                            pkt = node.pack_message(MsgType.HELLO_RESPONSE, sender_ip_real, resp_payload)
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
                            upstream_ip = node.handle_join(payload, sender_ip_real)
                            if upstream_ip and upstream_ip != "SOURCE":
                                pkt = node.pack_message(MsgType.STREAM_JOIN, upstream_ip, payload)
                                sock.sendto(pkt, (upstream_ip, DEFAULT_PORT))
                                print(f"[⬆️] Reencaminhando JOIN para {upstream_ip}")

                        elif header['type'] == MsgType.STREAM_LEAVE:
                            # Processar o LEAVE
                            upstream_to_prune = node.handle_leave(payload, sender_ip_real)
                            
                            # Se o nó disser que precisa de fazer Pruning, reencaminha para cima
                            if upstream_to_prune and upstream_to_prune != "SOURCE":
                                pkt = node.pack_message(MsgType.STREAM_LEAVE, upstream_to_prune, payload)
                                sock.sendto(pkt, (upstream_to_prune, DEFAULT_PORT))
                                print(f"[🚫] Reencaminhando LEAVE para upstream {upstream_to_prune}")

                        elif header['type'] == MsgType.STREAM_DATA:
                            try:
                                stream_info = json.loads(payload.decode('utf-8'))
                                s_id = stream_info.get('id')
                                
                                # --- LÓGICA DE CLIENTE: REMONTAGEM ---
                                if "C" in args.node_id:
                                    fid = stream_info.get('fid')
                                    cid = stream_info.get('cid')
                                    tot = stream_info.get('tot')
                                    chunk_data = stream_info.get('data')

                                    # Inicializar buffer para este frame se não existir
                                    if fid not in reassembly_buffer:
                                        reassembly_buffer[fid] = {}
                                    
                                    # Guardar pedaço
                                    reassembly_buffer[fid][cid] = chunk_data

                                    # Verificar se temos o frame completo
                                    if len(reassembly_buffer[fid]) == tot:
                                        # Reconstruir string base64 completa
                                        full_b64 = "".join([reassembly_buffer[fid][i] for i in range(tot)])
                                        
                                        # Descodificar para bytes
                                        img_bytes = base64.b64decode(full_b64)
                                        
                                        print(f"[📺] Frame {fid} COMPLETO ({len(img_bytes)} bytes)!")
                                        
                                        # Mostrar na GUI
                                        if gui:
                                            gui.update_image(img_bytes)
                                        
                                        # Limpar buffer (memória)
                                        del reassembly_buffer[fid]
                                        # Limpar frames muito velhos para não encher RAM
                                        old_frames = [k for k in reassembly_buffer.keys() if k < fid - 5]
                                        for k in old_frames: del reassembly_buffer[k]
                                
                                # --- LÓGICA DE ROUTER: FORWARDING ---
                                if s_id in node.routing_table:
                                    for child in node.routing_table[s_id].downstream_ips:
                                        if child != sender_ip_real:
                                            # Forwarding direto do pacote fragmentado
                                            pkt = node.pack_message(MsgType.STREAM_DATA, child, payload)
                                            sock.sendto(pkt, (child, DEFAULT_PORT))
                            except Exception as e: 
                                # print(f"Erro processamento frame: {e}")
                                pass

                    except Exception as e:
                        print(f"[ERRO] {e}")

                elif s is sys.stdin:
                    cmd = sys.stdin.readline().strip()
                    if cmd == "status":
                        print(f"Vizinhos (RTT): {node.neighbors}")
                        print(f"Rotas: {node.routing_table}")
                    elif cmd.startswith("join"):
                        parts = cmd.split()
                        if len(parts) > 1:
                            target = parts[1]
                            if target in node.routing_table:
                                next_hop = node.routing_table[target].proximo_salto_ip
                                payload = json.dumps({"stream_id": target}).encode('utf-8')
                                pkt = node.pack_message(MsgType.STREAM_JOIN, next_hop, payload)
                                sock.sendto(pkt, (next_hop, DEFAULT_PORT))
                                print(f"[🔌] Pedido JOIN enviado para {next_hop}")
                            else:
                                print("[!] Rota desconhecida. Espere pelo Flood.")
                                
                    elif cmd.startswith("leave"):
                        parts = cmd.split()
                        if len(parts) > 1:
                            target = parts[1]
                            if target in node.routing_table:
                                next_hop = node.routing_table[target].proximo_salto_ip
                                payload = json.dumps({"stream_id": target}).encode('utf-8')
                                pkt = node.pack_message(MsgType.STREAM_LEAVE, next_hop, payload)
                                sock.sendto(pkt, (next_hop, DEFAULT_PORT))
                                print(f"[✂️] Pedido LEAVE enviado para {next_hop}")
                                # Limpar buffer local se eu for cliente
                                if "C" in args.node_id:
                                    reassembly_buffer.clear()
                                    print("[INFO] Buffer de vídeo limpo.")

    except KeyboardInterrupt:
        print("A sair...")
    finally:
        if video_stream and video_stream.file: video_stream.file.close()
        sock.close()

if __name__ == "__main__":
    main()