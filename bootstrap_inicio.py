import os
import socket
import json
import threading

# --- CONFIGURAÇÃO ---
PORT = 5555 
DEFAULT_OVERLAY_PORT = 6000 # <--- MUDADO PARA 6000 (O padrão do teu projeto)
CONF_FILE = 'bootstrap_conf.json'

# Mapa: ID -> Lista de IPs de Vizinhos
TOPOLOGIA_VIZINHOS = {}

def load_config():
    global TOPOLOGIA_VIZINHOS
    try:
        if not os.path.exists(CONF_FILE):
             print(f"[ERRO] Ficheiro '{CONF_FILE}' não encontrado.")
             return False

        with open(CONF_FILE, 'r') as f:
            data = json.load(f)
        
        for node in data.get("nodes", []):
            node_id = node.get("id")
            neighbors = node.get("neighbors", [])
            if node_id:
                TOPOLOGIA_VIZINHOS[node_id] = neighbors
            
        print(f"[CONFIG] Topologia carregada para {len(TOPOLOGIA_VIZINHOS)} nós.")
        return True
    except Exception as e:
        print(f"[ERRO] JSON Inválido: {e}")
        return False

def handle_client(conn, addr):
    try:
        data = conn.recv(4096)
        if not data: return
        
        msg = json.loads(data.decode())
        node_id = msg.get('id')
        
        print(f"[REGISTO] Nó '{node_id}' a pedir vizinhos de {addr}")

        response_neighbors = []

        if node_id in TOPOLOGIA_VIZINHOS:
            lista_ips = TOPOLOGIA_VIZINHOS[node_id]
            
            for vizinho_ip in lista_ips:
                # O Bootstrapper envia o IP como "ID" temporário
                # O nó depois corrige isto quando receber o primeiro Heartbeat
                response_neighbors.append({
                    "id": vizinho_ip,
                    "ip": vizinho_ip, 
                    "port": DEFAULT_OVERLAY_PORT 
                })
            
            status = 'OK'
        else:
            print(f"[ERRO] Nó '{node_id}' não está no JSON.")
            status = 'ERRO'

        msg_resp = {'status': status, 'neighbors': response_neighbors}
        conn.sendall(json.dumps(msg_resp).encode('utf-8'))

    except Exception as e:
        print(f"[ERRO] {e}")
    finally:
        conn.close()

def start_server():
    if not load_config(): return
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("0.0.0.0", PORT))
        server.listen(5)
        print(f"Bootstrapper ON (Modo IPs) na porta {PORT}...")
        while True:
            client, addr = server.accept()
            threading.Thread(target=handle_client, args=(client, addr)).start()
    except Exception as e:
        print(f"[FATAL] {e}")
    finally:
        server.close()

if __name__ == '__main__':
    start_server()