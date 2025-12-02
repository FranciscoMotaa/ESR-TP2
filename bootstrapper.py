import socket
import threading
import json
import random
import os

# Configuração do Tracker
BIND_IP = "0.0.0.0"
BIND_PORT = 6000 # Porta TCP para registo

# Ficheiro de configuração com a topologia (node id -> lista de IPs)
CONF_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bootstrap_conf.json')
# Mapa carregado em memória: id -> [ip, ip, ...]
neighbor_map = {}

# Lista de nós activos: [{'id': 'R1', 'ip': '10.0.1.1'}, ...]
active_nodes = []
lock = threading.Lock()

def load_config():
    """Carrega `bootstrap_conf.json` e popula `neighbor_map` com a lista de vizinhos (IPs).
    O ficheiro tem a forma {"nodes": [{"id": "R1", "neighbors": ["10.0.0.2", ...]}, ...]}
    """
    global neighbor_map
    try:
        if not os.path.exists(CONF_FILE):
            print(f"[TRACKER] Aviso: ficheiro de configuração '{CONF_FILE}' não encontrado. A usar fallback.")
            neighbor_map = {}
            return
        with open(CONF_FILE, 'r') as f:
            data = json.load(f)
        nm = {}
        for node in data.get('nodes', []):
            nid = node.get('id')
            neigh = node.get('neighbors', []) or []
            if nid:
                nm[nid] = list(neigh)
        neighbor_map = nm
        print(f"[TRACKER] Topologia carregada de '{CONF_FILE}' para {len(neighbor_map)} nós.")
    except Exception as e:
        neighbor_map = {}
        print(f"[TRACKER] Erro ao carregar configuração: {e}")

def handle_client(client_sock, addr):
    try:
        # 1. Receber pedido de registo
        data = client_sock.recv(1024)
        if not data: return
        
        request = json.loads(data.decode('utf-8'))
        node_id = request.get('id')
        # aceitar também o porto no pedido
        node_ip = request.get('ip') or addr[0]
        node_port = request.get('port') or request.get('udp_port') or None
        if node_port is None:
            # se não foi enviado, tentamos obter da conexão TCP (não fiável)
            node_port = request.get('port')
        # normalizar para string ip:port quando possível
        if node_port:
            node_address = f"{node_ip}:{int(node_port)}"
        else:
            node_address = node_ip
        
        print(f"[TRACKER] Pedido de registo de {node_id} ({node_ip})")
        
        response_neighbors = []

        with lock:
            # 2. Adicionar/atualizar este nó na lista (para os próximos o encontrarem)
            existing = next((item for item in active_nodes if item["id"] == node_id), None)
            if not existing:
                entry = {'id': node_id, 'ip': node_ip}
                if node_port:
                    entry['address'] = node_address
                active_nodes.append(entry)
            else:
                existing['ip'] = node_ip
                if node_port:
                    existing['address'] = node_address

            # 3. Se a topologia estiver definida no ficheiro, devolve exatamente estes vizinhos.
            if node_id in neighbor_map:
                response_neighbors = list(neighbor_map.get(node_id, []))
                print(f"[TRACKER] Usando vizinhos do ficheiro para {node_id}: {response_neighbors}")
            else:
                # Fallback: escolher vizinhos aleatórios entre nós activos
                candidates = [n for n in active_nodes if n['id'] != node_id]
                if len(candidates) > 0:
                    k = min(len(candidates), 2)
                    selected = random.sample(candidates, k)
                    response_neighbors = [n.get('ip') for n in selected]
                
        # 4. Enviar resposta
        response = json.dumps({
            "status": "OK",
            "neighbors": response_neighbors
        })
        client_sock.send(response.encode('utf-8'))
        print(f"[TRACKER] {node_id} registado. Vizinhos atribuídos: {response_neighbors}")

    except Exception as e:
        print(f"[ERRO] {e}")
    finally:
        client_sock.close()

def start_tracker():
    # Carregar topologia a partir do ficheiro, se disponível
    load_config()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind((BIND_IP, BIND_PORT))
    server.listen(5)
    print(f"[*] Bootstrapper (Tracker) a correr em {BIND_IP}:{BIND_PORT}")

    while True:
        client, addr = server.accept()
        client_handler = threading.Thread(target=handle_client, args=(client, addr))
        client_handler.start()

if __name__ == "__main__":
    start_tracker()