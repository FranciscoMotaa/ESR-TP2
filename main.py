import socket
import sys
import select
import json
import argparse
import time
import base64
import math
import threading


from utils import get_interface_ip
from overlay_structs import OverlayNode, MsgType, MAXPACKETSIZE


try:
    import tkinter as tk
    from tkinter import Label
    HAS_GUI = True
except ImportError:
    HAS_GUI = False


try:
    import cv2
    HAS_MP4_SUPPORT = True
except ImportError:
    HAS_MP4_SUPPORT = False
    print("[WARN] OpenCV não instalado. Use Mjpeg.")


# CONSTANTES
DEFAULT_PORT = 50000
BOOTSTRAP_PORT = 6000
VIDEO_FILE = "video1.mp4"
TITLE = "Overlay ESR"


# CLASSES

class VideoStream:
    """Lê ficheiro Mjpeg"""
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
        if not self.file: 
            return None
        try:
            data = self.file.read(5)
            if not data: 
                self.file.seek(0) 
                data = self.file.read(5)
            framelength = int(data)
            data = self.file.read(framelength)
            self.frame_num += 1
            return data
        except: 
            return None

    def frame_nbr(self):
        return self.frame_num


class VideoStreamMP4:
    """Lê vídeo MP4 com OpenCV"""
    def __init__(self, filename):
        self.filename = filename
        self.cap = cv2.VideoCapture(filename)
        
        if not self.cap.isOpened():
            raise IOError(f"[VIDEO] Não consegui abrir {filename}")
        
        self.frame_num = 0
        self.fps = self.cap.get(cv2.CAP_PROP_FPS)
        print(f"[VIDEO] Ficheiro aberto: {filename}")
        print(f"[VIDEO] FPS: {self.fps:.1f}")
    
    def next_frame(self):
        """Retorna próximo frame em JPEG"""
        ret, frame = self.cap.read()
        
        if not ret:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, frame = self.cap.read()
        
        if ret:
            ret, jpeg = cv2.imencode('.jpg', frame, 
                                    [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ret:
                self.frame_num += 1
                return jpeg.tobytes()
        
        return None
    
    def frame_nbr(self):
        return self.frame_num
    
    def close(self):
        if self.cap:
            self.cap.release()


class VideoGUI:
    """GUI para visualizar vídeo com Tkinter"""
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


# FUNÇÕES DE BOOTSTRAP

def get_neighbors_dynamic(tracker_ip, my_id, my_ip, my_port=None):
    """Conecta ao Tracker e recebe vizinhos iniciais"""
    print(f"[*] A contactar Tracker em {tracker_ip}:{BOOTSTRAP_PORT}...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3.0)
        sock.connect((tracker_ip, BOOTSTRAP_PORT))

        request = json.dumps({"id": my_id, "ip": my_ip, "port": my_port})
        sock.send(request.encode('utf-8'))
        
        data = sock.recv(4096)
        response = json.loads(data.decode('utf-8'))
        
        sock.close()
        
        if response.get("status") == "OK":
            neighbors = response.get("neighbors", [])
            print(f"[*] Vizinhos iniciais: {neighbors}")
            return neighbors
        else:
            print("[!] Erro ao obter vizinhos")
            return []
            
    except Exception as e:
        print(f"[ERRO] Falha ao contactar Tracker: {e}")
        sys.exit(1)


#  FUNÇÃO PARA GUARDAR ESTADO 
def save_network_state(node_id, neighbors, routing_table):
    """Guarda estado atual da rede em ficheiro"""
    state = {
        "node_id": node_id,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "neighbors": {
            ip: {
                "metric": data.get("metric", 0),
                "lastseen": data.get("lastseen", 0)
            }
            for ip, data in neighbors.items()
        },
        "num_neighbors": len(neighbors),
        "routing_table": {
            stream_id: {
                "source": entry.source_id,
                "next_hop": entry.proximos_salto_ip,
                "cost": entry.custo_acumulado,
                "downstream": list(entry.downstream_ips)
            }
            for stream_id, entry in routing_table.items()
        },
        "num_routes": len(routing_table)
    }
    
    filename = f"state_{node_id}.json"
    with open(filename, 'w') as f:
        json.dump(state, f, indent=2)
    
    print(f"[STATE] Guardado em {filename}")


# MAIN

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("node_id")
    parser.add_argument("--tracker", help="IP do Bootstrapper", required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="UDP port para o nó")
    parser.add_argument("--target-stream", help="ID do stream a pedir (ex: STREAMER1)")
    args = parser.parse_args()

    my_ip = get_interface_ip()
    print(f"[*] Nó {args.node_id} ({my_ip}) a iniciar...")

    # Obter vizinhos do Tracker
    initial_neighbors = get_neighbors_dynamic(args.tracker, args.node_id, my_ip, args.port)
    
    node = OverlayNode(args.node_id, my_ip, args.port)
    
    # Adicionar os vizinhos que o Tracker devolveu
    for neighbor in initial_neighbors:
        node.neighbors[neighbor] = {"metric": 50.0, "lastseen": 0}
    
    # GUARDA ESTADO INICIAL
    save_network_state(args.node_id, node.neighbors, node.routing_table)

    if not initial_neighbors:
        print("[*] Sou o primeiro nó. À espera de conexões...")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', args.port))
    sock.setblocking(0)

    def dest_tuple(nip):
        # nip pode ser 'ip:port' ou apenas 'ip'
        if isinstance(nip, str) and ':' in nip:
            ip, p = nip.rsplit(':', 1)
            try:
                return (ip, int(p))
            except:
                return (nip, args.port)
        return (nip, args.port)

    # Se for cliente e foi pedido um stream, envia JOIN inicial aos vizinhos
    if "C" in args.node_id and args.target_stream:
        try:
            payload = json.dumps({"stream_id": args.target_stream}).encode('utf-8')
            for nip in list(node.neighbors.keys()):
                pkt = node.pack_message(MsgType.STREAM_JOIN, nip, payload)
                sock.sendto(pkt, dest_tuple(nip))
                print(f"[*] CLIENT {args.node_id} enviou JOIN para {args.target_stream} via {nip}")
        except Exception as e:
            print(f"[ERRO JOIN] {e}")

    # INICIALIZA VÍDEO
    video_stream = None
    gui = None

    if "STREAMER" in args.node_id:
        try:
            if VIDEO_FILE.endswith('.mp4') and HAS_MP4_SUPPORT:
                print(f"[VIDEO] A tentar MP4: {VIDEO_FILE}")
                video_stream = VideoStreamMP4(VIDEO_FILE)
            else:
                print(f"[VIDEO] A tentar Mjpeg: {VIDEO_FILE}")
                video_stream = VideoStream(VIDEO_FILE)
        except Exception as e:
            print(f"[VIDEO] Erro ao carregar vídeo: {e}")
            video_stream = None

    if "C" in args.node_id and HAS_GUI:
        try:
            root = tk.Tk()
            gui = VideoGUI(root, f"Cliente {args.node_id}")
            print("[GUI] Janela de vídeo iniciada.")
        except Exception as e:
            print(f"[GUI] Falha ao iniciar janela gráfica: {e}")
            gui = None

    reassembly_buffer = {}
    last_hello = 0
    last_flood = 0
    last_frame = 0
    last_save = 0  # PARA GUARDAR ESTADO
    
    HELLO_INTERVAL = 1.0
    FLOOD_INTERVAL = 10.0
    FRAME_INTERVAL = 0.05
    CHUNK_SIZE = 1024

    inputs = [sock, sys.stdin]
    is_streamer = "STREAMER" in args.node_id
    frame_seq = 0

    print("[*] Loop Overlay iniciado. Ctrl+C para sair.")

    try:
        while True:
            now = time.time()

            # --- A. HELLO ---
            if now - last_hello >= HELLO_INTERVAL:
                for nip in list(node.neighbors.keys()):
                    pkt = node.pack_message(MsgType.HELLO, nip, b'')
                    node.pending_pings[node.sequence_number] = now
                    sock.sendto(pkt, dest_tuple(nip))
                last_hello = now

            # --- B. FLOOD ---
            if is_streamer and now - last_flood >= FLOOD_INTERVAL:
                flood_payload = json.dumps({
                    "stream_id": args.node_id,
                    "cost": 0,
                    "origin_seq": int(now)
                }).encode('utf-8')
                print(f"[*] A iniciar Flood para {args.node_id}")
                
                for nip in list(node.neighbors.keys()):
                    pkt = node.pack_message(MsgType.ROUTE_DISCOVERY, nip, flood_payload)
                    sock.sendto(pkt, dest_tuple(nip))
                last_flood = now

            # --- C. VÍDEO ---
            if is_streamer and video_stream and now - last_frame >= FRAME_INTERVAL:
                if args.node_id in node.routing_table:
                    clients = node.routing_table[args.node_id].downstream_ips
                    
                    raw_bytes = video_stream.next_frame()
                    if raw_bytes:
                        frame_seq += 1
                        b64_data = base64.b64encode(raw_bytes).decode('utf-8')
                        total_len = len(b64_data)
                        num_chunks = math.ceil(total_len / CHUNK_SIZE)
                        
                        for i in range(num_chunks):
                            start = i * CHUNK_SIZE
                            end = start + CHUNK_SIZE
                            chunk_data = b64_data[start:end]
                            
                            payload = json.dumps({
                                "id": args.node_id,
                                "fid": frame_seq,
                                "cid": i,
                                "tot": num_chunks,
                                "data": chunk_data
                            }).encode('utf-8')
                            
                            for client_ip in list(clients):
                                pkt = node.pack_message(MsgType.STREAM_DATA, client_ip, payload)
                                sock.sendto(pkt, dest_tuple(client_ip))
                
                last_frame = now

            # ← GUARDA ESTADO A CADA 10 SEGUNDOS
            if int(now) % 10 == 0 and int(now) % 10 != last_save:
                save_network_state(args.node_id, node.neighbors, node.routing_table)
                last_save = int(now)

            # --- EVENT LOOP ---
            readable, _, _ = select.select(inputs, [], [], 0.01)
            
            for s in readable:
                if s is sock:
                    try:
                        data, addr = sock.recvfrom(MAXPACKETSIZE)
                        # addr = (ip, port)
                        sender_addr = f"{addr[0]}:{addr[1]}"

                        if sender_addr not in node.neighbors:
                            print(f"[AUTO-DISCOVERY] Novo vizinho: {sender_addr}")
                            node.neighbors[sender_addr] = {"metric": 50.0, "lastseen": 0}

                        header, payload = node.unpack_message(data)
                        
                        if not header:
                            continue
                        
                        if header["type"] == MsgType.HELLO:
                            res_payload = node.handle_hello(header, sender_addr)
                            pkt = node.pack_message(MsgType.HELLO_RESPONSE, sender_addr, res_payload)
                            sock.sendto(pkt, dest_tuple(sender_addr))

                        elif header["type"] == MsgType.HELLO_RESPONSE:
                            node.handle_hello_response(payload, sender_addr)

                        elif header["type"] == MsgType.ROUTE_DISCOVERY:
                            new_payload = node.handle_flood(header, payload, sender_addr)
                            if new_payload:
                                for nip in list(node.neighbors.keys()):
                                    if nip != sender_addr:
                                        pkt = node.pack_message(MsgType.ROUTE_DISCOVERY, nip, new_payload)
                                        sock.sendto(pkt, dest_tuple(nip))

                        elif header["type"] == MsgType.STREAM_JOIN:
                            upstream_ip = node.handle_join(payload, sender_addr)
                            if upstream_ip and upstream_ip != "SOURCE":
                                pkt = node.pack_message(MsgType.STREAM_JOIN, upstream_ip, payload)
                                sock.sendto(pkt, dest_tuple(upstream_ip))
                                print(f"[*] Reencaminhando JOIN para {upstream_ip}")

                        elif header["type"] == MsgType.STREAM_LEAVE:
                            upstream_to_prune = node.handle_leave(payload, sender_addr)
                            if upstream_to_prune and upstream_to_prune != "SOURCE":
                                pkt = node.pack_message(MsgType.STREAM_LEAVE, upstream_to_prune, payload)
                                sock.sendto(pkt, dest_tuple(upstream_to_prune))
                                print(f"[*] Reencaminhando LEAVE para {upstream_to_prune}")

                        elif header["type"] == MsgType.STREAM_DATA:
                            try:
                                stream_info = json.loads(payload.decode('utf-8'))
                                sid = stream_info.get("id")
                                fid = stream_info.get("fid")
                                cid = stream_info.get("cid")
                                tot = stream_info.get("tot")
                                chunk_data = stream_info.get("data")
                                
                                if fid not in reassembly_buffer:
                                    reassembly_buffer[fid] = {}
                                
                                reassembly_buffer[fid][cid] = chunk_data
                                
                                if len(reassembly_buffer[fid]) == tot:
                                    full_b64 = "".join(reassembly_buffer[fid][i] for i in range(tot))
                                    img_bytes = base64.b64decode(full_b64)
                                    print(f"[STREAM] Frame {fid} COMPLETO {len(img_bytes)} bytes!")
                                    
                                    if gui:
                                        gui.update_image(img_bytes)
                                    
                                    del reassembly_buffer[fid]
                                
                                if sid in node.routing_table:
                                    for child in node.routing_table[sid].downstream_ips:
                                        if child != sender_addr:
                                            pkt = node.pack_message(MsgType.STREAM_DATA, child, payload)
                                            sock.sendto(pkt, dest_tuple(child))
                            
                            except Exception as e:
                                print(f"[ERRO STREAM] {e}")
                        
                    except Exception as e:
                        print(f"[ERRO] {e}")
                
                elif s is sys.stdin:
                    cmd = sys.stdin.readline().strip()
                    if cmd == "status":
                        print(f"[*] Vizinhos: {node.neighbors}")
                        print(f"[*] Rotas: {node.routing_table}")

    except KeyboardInterrupt:
        print("[*] A sair...")
        #  GUARDA ESTADO FINAL
        save_network_state(args.node_id, node.neighbors, node.routing_table)
    finally:
        if video_stream:
            if hasattr(video_stream, 'close'):
                video_stream.close()
        sock.close()


if __name__ == "__main__":
    main()
