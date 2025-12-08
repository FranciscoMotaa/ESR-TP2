import socket
import threading
import json
import sys
import time
from PIL import Image, ImageTk

# Configuração do Tracker
BIND_IP = "0.0.0.0"
BIND_PORT = 6000
TOPOLOGY_FILE = "bootstrap_conf.json"
HEARTBEAT_TIMEOUT = 15  # segundos para considerar ALIVE
MONITOR_INTERVAL = 5    # segundos entre impressões da tabela

# Topologia estática (do JSON)
static_topology = {}

# Nós activos: { node_id: { 'ip': str, 'last_seen': timestamp, 'neighbors': [...] } }
active_nodes = {}
lock = threading.Lock()


def load_topology():
    global static_topology
    try:
        with open(TOPOLOGY_FILE, 'r') as f:
            data = json.load(f)
            for node in data.get("nodes", []):
                node_id = node.get("id")
                neighbors = node.get("neighbors", [])
                static_topology[node_id] = neighbors
        print(f"[*] Topologia carregada: {len(static_topology)} nós configurados.")
    except Exception as e:
        print(f"[ERRO] Falha ao carregar {TOPOLOGY_FILE}: {e}")
        sys.exit(1)


def print_status_table():
    """Imprime uma tabela simples com o estado ALIVE/DEAD de todos os nós configurados."""
    with lock:
        ids = sorted(set(list(static_topology.keys()) + list(active_nodes.keys())))
        rows = []
        alive = 0
        for nid in ids:
            entry = active_nodes.get(nid)
            cfg_neighbors = static_topology.get(nid, [])
            if entry:
                last_seen = entry.get('last_seen', 0)
                ip = entry.get('ip', '')
                delta = int(time.time() - last_seen)
                state = 'ALIVE' if delta <= HEARTBEAT_TIMEOUT else 'DEAD'
                last_seen_str = f"{delta}s ago" if state == 'ALIVE' else '-'
                neighbors = entry.get('neighbors', []) or []
            else:
                ip = ''
                state = 'DEAD'
                last_seen_str = '-'
                neighbors = []

            if state == 'ALIVE':
                alive += 1

            rows.append((nid, ip, state, last_seen_str, neighbors))

    # Impressão
    print('\n' + '='*80)
    print(f"{'ID':<12} {'IP':<18} {'STATE':<8} {'LAST SEEN':<12} {'NEIGHBORS':<30}")
    print('-'*110)
    for r in rows:
        neigh_str = ','.join(r[4]) if r[4] else '-'
        if len(neigh_str) > 28:
            neigh_str = neigh_str[:25] + '...'
        print(f"{r[0]:<12} {r[1]:<18} {r[2]:<8} {r[3]:<12} {neigh_str:<30}")
    print('-'*80)
    print(f"Total: {len(rows)} | Alive: {alive} | Dead: {len(rows)-alive}")
    print('='*80 + '\n')


def monitor_thread():
    while True:
        time.sleep(MONITOR_INTERVAL)
        print_status_table()


def handle_client(client_sock, addr):
    try:
        data = client_sock.recv(4096)
        if not data:
            return

        request = json.loads(data.decode('utf-8'))
        node_id = request.get('id')
        node_ip = request.get('ip')

        print(f"[TRACKER] Pedido de registo de {node_id} ({node_ip})")

        # Registar/actualizar timestamp e gerir vizinhos conhecidos
        with lock:
            configured = static_topology.get(node_id, [])
            # inicializar estrutura do nó se não existir
            if node_id not in active_nodes:
                active_nodes[node_id] = {
                    'ip': node_ip,
                    'last_seen': time.time(),
                    'configured_neighbors': configured,
                    'neighbors': []
                }
            else:
                # actualizar ip e timestamp
                active_nodes[node_id]['ip'] = node_ip
                active_nodes[node_id]['last_seen'] = time.time()
                # garantir campo configured_neighbors
                active_nodes[node_id].setdefault('configured_neighbors', configured)

            # Descobrir quais dos seus vizinhos configurados já estão activos
            known = []
            active_ips = {v['ip'] for k, v in active_nodes.items() if v.get('ip')}
            for ip in configured:
                if ip in active_ips and ip != node_ip:
                    known.append(ip)
            active_nodes[node_id]['neighbors'] = known

            # Atualizar outros nós: se algum outro nó tem este node_ip nos seus configured_neighbors,
            # então acrescentar como vizinho conhecido desse nó
            for other_id, other in active_nodes.items():
                if other_id == node_id:
                    continue
                other_configured = other.get('configured_neighbors', [])
                if node_ip in other_configured and node_ip not in other.get('neighbors', []):
                    other.setdefault('neighbors', []).append(node_ip)

            response_neighbors = active_nodes[node_id]['neighbors']

        response = json.dumps({"status": "OK", "neighbors": response_neighbors})
        client_sock.send(response.encode('utf-8'))

        print(f"[TRACKER] {node_id} registado/atualizado. Vizinhos conhecidos: {response_neighbors}")

    except Exception as e:
        print(f"[ERRO] {e}")
    finally:
        try:
            client_sock.close()
        except:
            pass


def start_tracker():
    load_topology()

    # Iniciar thread de monitorização
    t = threading.Thread(target=monitor_thread, daemon=True)
    t.start()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((BIND_IP, BIND_PORT))
    server.listen(5)
    print(f"[*] Bootstrapper (Tracker) a correr em {BIND_IP}:{BIND_PORT}")

    try:
        while True:
            client, addr = server.accept()
            threading.Thread(target=handle_client, args=(client, addr), daemon=True).start()
    except KeyboardInterrupt:
        print('\n[*] A terminar Bootstrapper...')
    finally:
        server.close()


if __name__ == "__main__":
    start_tracker()
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