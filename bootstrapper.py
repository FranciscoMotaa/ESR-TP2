import socket
import threading
import json
import sys

# Configuração do Tracker
BIND_IP = "0.0.0.0"
BIND_PORT = 6000
TOPOLOGY_FILE = "bootstrap_conf.json"
static_topology = {}

def load_topology():
    global static_topology
    try:
        with open(TOPOLOGY_FILE, 'r') as f:
            data = json.load(f)
            for node in data.get("nodes", []):
                node_id = node["id"]
                neighbors = node["neighbors"]
                static_topology[node_id] = neighbors
        print(f"[*] Topologia carregada: {len(static_topology)} nós configurados.")
    except Exception as e:
        print(f"[ERRO] Falha ao carregar {TOPOLOGY_FILE}: {e}")
        sys.exit(1)

def handle_client(client_sock, addr):
    try:
        data = client_sock.recv(1024)
        if not data: return
        
        request = json.loads(data.decode('utf-8'))
        node_id = request.get('id')
        node_ip = request.get('ip') 
        
        print(f"[TRACKER] Pedido de registo de {node_id} ({node_ip})")
        
        response_neighbors = []
        
        # Modo Determinístico: Ler do JSON
        if node_id in static_topology:
            response_neighbors = static_topology[node_id]
            print(f"   -> Vizinhos definidos no JSON: {response_neighbors}")
        else:
            print(f"   -> [AVISO] Nó {node_id} não encontrado no JSON. Devolvendo lista vazia.")

        response = json.dumps({"status": "OK", "neighbors": response_neighbors})
        client_sock.send(response.encode('utf-8'))

    except Exception as e:
        print(f"[ERRO] {e}")
    finally:
        client_sock.close()

def start_tracker():
    load_topology()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind((BIND_IP, BIND_PORT))
    server.listen(5)
    print(f"[*] Bootstrapper Determinístico a correr em {BIND_IP}:{BIND_PORT}")
    
    while True:
        client, addr = server.accept()
        threading.Thread(target=handle_client, args=(client, addr)).start()

if __name__ == "__main__":
    start_tracker()