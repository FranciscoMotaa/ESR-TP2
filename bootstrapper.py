import socket
import threading
import json
import sys
import time
import os
from datetime import datetime
from collections import defaultdict

# Configuração do Tracker
BIND_IP = "0.0.0.0"
BIND_PORT = 6000
MONITOR_PORT = 6001  # Porta UDP para receber updates de estado
NODE_DEFAULT_PORT = 50000  # Porta que os nós usam para comunicação
TOPOLOGY_FILE = "bootstrap_conf.json"

# Estruturas de dados para monitorização
node_state = {}
route_history = {}
neighbor_discovery = {}
static_topology = {} # Guarda a configuração do ficheiro JSON
state_lock = threading.Lock()


def load_topology():
    """Carrega a topologia estrita do ficheiro JSON."""
    global static_topology
    try:
        with open(TOPOLOGY_FILE, 'r') as f:
            data = json.load(f)
            for node in data.get("nodes", []):
                static_topology[node["id"]] = node["neighbors"]
        print(f"[*] Topologia carregada de {TOPOLOGY_FILE}: {len(static_topology)} nós configurados.")
    except Exception as e:
        print(f"[ERRO] Falha ao carregar {TOPOLOGY_FILE}: {e}")
        print("[AVISO] O Tracker vai funcionar, mas sem restrições de topologia!")


def notify_new_node(new_node_id, new_node_ip):
    """
    Notifica APENAS os vizinhos legítimos (definidos no JSON) sobre o novo nó.
    """
    notification = json.dumps({
        "type": "neighbor_update",
        "new_neighbor": new_node_ip,
        "neighbor_id": new_node_id
    }).encode("utf-8")

    with state_lock:
        for other_node_id, info in node_state.items():
            if other_node_id == new_node_id: continue
            
            # --- FILTRO DE TOPOLOGIA (CORREÇÃO CRÍTICA) ---
            # Só notifica se estiverem ligados no JSON
            allowed_neighbors_ips = static_topology.get(other_node_id, [])
            
            if new_node_ip in allowed_neighbors_ips:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.settimeout(0.5)
                    other_ip = info.get("ip")
                    if other_ip:
                        sock.sendto(notification, (other_ip, NODE_DEFAULT_PORT))
                    sock.close()
                    print(f"   -> Notificado {other_node_id} sobre vizinho {new_node_id}")
                except Exception: pass


def handle_client(client_sock, addr):
    """Lida com pedidos de registo TCP (Bootstrapping Estrito)."""
    try:
        data = client_sock.recv(4096)
        if not data: return
        try: request = json.loads(data.decode("utf-8"))
        except: return

        node_id = request.get("id")
        node_ip = request.get("ip")

        response_neighbors = []
        is_new_registration = False

        with state_lock:
            # 1. Regista/Atualiza nó no estado
            if node_id not in node_state:
                node_state[node_id] = {
                    "ip": node_ip,
                    "ips": [node_ip],
                    "last_seen": time.time(),
                    "neighbors": {},
                    "routing_table": {},
                    "streams": []
                }
                is_new_registration = True
            else:
                node_state[node_id]["ip"] = node_ip
                node_state[node_id]["last_seen"] = time.time()

            # 2. FILTRO DE VIZINHOS (CORREÇÃO CRÍTICA)
            # Devolve apenas os vizinhos que estão no JSON
            response_neighbors = static_topology.get(node_id, [])

        response = json.dumps({"status": "OK", "neighbors": response_neighbors})
        try: client_sock.send(response.encode("utf-8"))
        except: pass

        if is_new_registration:
            threading.Thread(target=notify_new_node, args=(node_id, node_ip), daemon=True).start()

    except Exception: pass
    finally:
        try: client_sock.close()
        except: pass


def handle_state_update(sock):
    while True:
        try:
            data, addr = sock.recvfrom(8192)
            try: update = json.loads(data.decode("utf-8"))
            except: continue

            node_id = update.get("node_id")
            if not node_id: continue

            with state_lock:
                if node_id not in node_state:
                    node_state[node_id] = {
                        "ip": addr[0],
                        "ips": [addr[0]],
                        "last_seen": time.time(),
                        "neighbors": {},
                        "routing_table": {},
                        "streams": []
                    }
                    neighbor_discovery[node_id] = {}
                    route_history[node_id] = {}

                current_time = time.time()
                
                # Atualizar lista de IPs para visualização correta
                if "ips" in update: node_state[node_id]["ips"] = update["ips"]

                new_neighbors = update.get("neighbors", {})
                if node_id in neighbor_discovery:
                    for neighbor_ip in new_neighbors:
                        if neighbor_ip not in neighbor_discovery[node_id]:
                            neighbor_discovery[node_id][neighbor_ip] = current_time

                new_routing_table = update.get("routing_table", {})
                old_routing_table = node_state[node_id].get("routing_table", {})

                for dest_id, new_route in new_routing_table.items():
                    new_next_hop = new_route.get("next_hop")
                    new_cost = new_route.get("cost", 0)

                    if node_id not in route_history: route_history[node_id] = {}
                    if dest_id not in route_history[node_id]: route_history[node_id][dest_id] = []

                    if dest_id in old_routing_table:
                        old_next_hop = old_routing_table[dest_id].get("next_hop")
                        if new_next_hop != old_next_hop:
                            route_history[node_id][dest_id].append((current_time, new_next_hop, new_cost))
                    else:
                        route_history[node_id][dest_id].append((current_time, new_next_hop, new_cost))

                node_state[node_id]["last_seen"] = current_time
                node_state[node_id]["neighbors"] = new_neighbors
                node_state[node_id]["routing_table"] = new_routing_table
                node_state[node_id]["streams"] = update.get("streams", [])

        except Exception: time.sleep(0.1)

# --- FUNÇÕES DE ÁRVORE ---
def find_node_id_by_ip(target_ip):
    for nid, data in node_state.items():
        known_ips = data.get('ips', [])
        if target_ip == data.get('ip') or target_ip in known_ips:
            return nid
    return target_ip 

def print_tree(node_id, stream_id, prefix="", is_last=True, visited=None):
    if visited is None: visited = []
    connector = "└── " if is_last else "├── "
    suffix = " (Cliente)" if "C" in node_id and "STREAMER" not in node_id else ""
    print(f"{prefix}{connector}{node_id}{suffix}")
    
    new_prefix = prefix + ("    " if is_last else "│   ")
    
    children = []
    if node_id in node_state:
        routing_table = node_state[node_id].get('routing_table', {})
        route_info = routing_table.get(stream_id, {})
        downstream_ips = route_info.get('downstream', [])
        for ip in downstream_ips:
            child_id = find_node_id_by_ip(ip)
            if child_id != node_id: children.append(child_id)
        children.sort()

    for i, child_id in enumerate(children):
        is_last_child = (i == len(children) - 1)
        if child_id in visited:
            print(f"{new_prefix}└── {child_id} [LOOP]")
        else:
            print_tree(child_id, stream_id, new_prefix, is_last_child, visited + [node_id])

def display_monitor():
    while True:
        time.sleep(2)
        with state_lock:
            if not node_state: continue
            print("\033[2J\033[H", end="")
            now = time.time()
            print("=" * 100)
            print(f"MONITORIZAÇÃO TOPOLOGIA ESTRITA - {datetime.now().strftime('%H:%M:%S')}")
            print("=" * 100)

            # TABELA NÓS
            print(f"{'ID':<10} {'STATUS':<8} {'IP':<15} {'VIZINHOS ATIVOS'}")
            active_streamers = []
            for nid in sorted(node_state.keys()):
                info = node_state[nid]
                alive = (now - info['last_seen']) < 10
                status = "ONLINE" if alive else "OFF"
                neighbors = info.get('neighbors', {})
                
                active_neighbors = []
                for n_ip, data_or_cost in neighbors.items():
                    n_id = find_node_id_by_ip(n_ip) 
                    if n_id == n_ip: n_id = "..." + n_ip[-3:]
                    cost = 0
                    if isinstance(data_or_cost, dict): cost = data_or_cost.get('metric', 0)
                    else:
                        try: cost = float(data_or_cost)
                        except: pass
                    active_neighbors.append(f"{n_id}({cost:.0f}ms)")
                
                print(f"{nid:<10} {status:<8} {info.get('ip'):<15} {', '.join(active_neighbors)}")
                
                if "STREAMER" in nid and alive: active_streamers.append(nid)

            # ÁRVORE
            print("\n" + "="*100)
            print(" ÁRVORE DE MULTICAST (Caminho Visual)")
            print("="*100)
            if not active_streamers: print("[!] Sem Streamers.")
            else:
                for s_id in active_streamers:
                    print(f"\n📺 Fonte: {s_id}")
                    print_tree(s_id, s_id, "", True)

        print("\n(Ctrl+C para sair)")

def start_tracker():
    load_topology() # Carrega o JSON
    
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try: server.bind((BIND_IP, BIND_PORT))
    except: sys.exit(1)
    server.listen(5)

    monitor_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: monitor_sock.bind((BIND_IP, MONITOR_PORT))
    except: sys.exit(1)

    threading.Thread(target=handle_state_update, args=(monitor_sock,), daemon=True).start()
    threading.Thread(target=display_monitor, daemon=True).start()

    while True:
        try:
            client, addr = server.accept()
            threading.Thread(target=handle_client, args=(client, addr), daemon=True).start()
        except KeyboardInterrupt: raise

if __name__ == "__main__":
    try: start_tracker()
    except: sys.exit(0)