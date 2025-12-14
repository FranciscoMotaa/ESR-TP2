import socket
import threading
import json
import sys
import time
from datetime import datetime
from collections import defaultdict

# --- CONFIGURAÇÃO ---
BIND_IP = "0.0.0.0"
BIND_PORT = 6000
MONITOR_PORT = 6001  # Porta UDP para receber updates de estado
NODE_DEFAULT_PORT = 50000 # Porta padrão dos nós

# --- ESTRUTURAS DE DADOS ---
# node_state: {node_id: {'ip': ..., 'last_seen': ..., 'neighbors': {...}, 'routing_table': {...}, 'streams': [...]}}
node_state = {}
route_history = {}
neighbor_discovery = {}
state_lock = threading.Lock()

# --- FUNÇÕES AUXILIARES ---

def load_topology():
    """Inicializa o Tracker (Modo Dinâmico)."""
    print("[*] Tracker inicializado. Descoberta de vizinhos dinâmica ativa.")

def find_node_id_by_ip(target_ip):
    """Encontra o ID do nó dado o seu IP."""
    for nid, data in node_state.items():
        # Verifica o IP principal
        if target_ip == data.get('ip'):
            return nid
        # Se houver lista de IPs (multi-interface), verifica também
        if target_ip in data.get('ips', []):
            return nid
    return target_ip  # Se não encontrar, devolve o próprio IP

def print_tree(node_id, stream_id, prefix="", is_last=True, visited=None):
    """Imprime a árvore de multicast recursivamente."""
    if visited is None: visited = []
    
    connector = "└── " if is_last else "├── "
    
    # Marcador visual se for cliente (destino final)
    suffix = ""
    if "C" in node_id and "STREAMER" not in node_id:
        suffix = " \033[92m(Cliente)\033[0m" # Verde se suportado pelo terminal
    
    print(f"{prefix}{connector}{node_id}{suffix}")
    
    new_prefix = prefix + ("    " if is_last else "│   ")
    
    children = []
    if node_id in node_state:
        routing_table = node_state[node_id].get('routing_table', {})
        # Assume que a chave na routing table é o stream_id (ou node origem)
        route_info = routing_table.get(stream_id, {})
        # aceitar ambas as formas ('downstream' enviado por main.py, ou 'downstream_ips')
        downstream_ips = route_info.get('downstream', None)
        if downstream_ips is None:
            downstream_ips = route_info.get('downstream_ips', [])
        # garantir lista
        if downstream_ips is None:
            downstream_ips = []

        
        for ip in downstream_ips:
            child_id = find_node_id_by_ip(ip)
            # Evita adicionar o próprio nó como filho (loop local)
            if child_id != node_id: 
                children.append(child_id)
        
        children.sort()

    for i, child_id in enumerate(children):
        is_last_child = (i == len(children) - 1)
        if child_id in visited:
            print(f"{new_prefix}└── {child_id} [LOOP DETECTADO]")
        else:
            print_tree(child_id, stream_id, new_prefix, is_last_child, visited + [node_id])

# --- LÓGICA DE REGISTO (TCP) ---

def notify_new_node(new_node_id, new_node_ip):
    """Notifica todos os nós ativos sobre a entrada de um novo nó."""
    notification = json.dumps({
        "type": "neighbor_update",
        "new_neighbor": new_node_ip,
        "neighbor_id": new_node_id
    }).encode('utf-8')

    print(f"[*] A notificar rede sobre novo nó {new_node_id} ({new_node_ip}).")

    with state_lock:
        for other_node_id, info in node_state.items():
            if other_node_id != new_node_id:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.settimeout(0.2)
                    other_ip = info['ip']
                    sock.sendto(notification, (other_ip, NODE_DEFAULT_PORT))
                    sock.close()
                except Exception:
                    pass

def handle_client(client_sock, addr):
    """Lida com registo TCP (Bootstrap)."""
    try:
        data = client_sock.recv(1024)
        if not data: return

        request = json.loads(data.decode('utf-8'))
        node_id = request.get('id')
        node_ip = request.get('ip')

        # print(f"[TRACKER] Registo: {node_id} ({node_ip})")

        response_neighbors = []
        is_new_registration = False

        with state_lock:
            # Enviar IPs de todos os nós conhecidos como vizinhos potenciais
            all_active_ips = [info['ip'] for n_id, info in node_state.items() if n_id != node_id]
            # Evitar devolver IPs de STREAMER diretamente a clientes
            is_client = isinstance(node_id, str) and node_id.startswith('C')
            filtered = []
            for ip in all_active_ips:
                nid = find_node_id_by_ip(ip)
                if is_client and isinstance(nid, str) and 'STREAMER' in nid:
                    continue
                filtered.append(ip)
            response_neighbors = filtered

            if node_id not in node_state:
                node_state[node_id] = {
                    'ip': node_ip,
                    'last_seen': time.time(),
                    'neighbors': {},
                    'routing_table': {},
                    'streams': []
                }
                is_new_registration = True
            else:
                node_state[node_id]['ip'] = node_ip
                node_state[node_id]['last_seen'] = time.time()

        response = json.dumps({"status": "OK", "neighbors": response_neighbors})
        client_sock.send(response.encode('utf-8'))

        if is_new_registration:
            threading.Thread(target=notify_new_node, args=(node_id, node_ip), daemon=True).start()

    except Exception as e:
        print(f"[ERRO HANDLER] {e}")
    finally:
        try: client_sock.close()
        except: pass

# --- MONITORIZAÇÃO (UDP) ---

def handle_state_update(sock):
    """Processa updates de estado recebidos via UDP."""
    while True:
        try:
            data, addr = sock.recvfrom(8192)
            try:
                update = json.loads(data.decode('utf-8'))
            except: continue

            node_id = update.get('node_id')
            if not node_id: continue

            with state_lock:
                current_time = time.time()
                
                # Se o nó não existe (skipou TCP), cria entrada básica
                if node_id not in node_state:
                    node_state[node_id] = {'ip': addr[0], 'routing_table': {}, 'neighbors': {}, 'streams': []}
                
                # Atualiza Estado
                node_state[node_id]['last_seen'] = current_time
                node_state[node_id]['neighbors'] = update.get('neighbors', {})
                node_state[node_id]['routing_table'] = update.get('routing_table', {})
                node_state[node_id]['streams'] = update.get('streams', [])
                
                # Atualiza IP se mudou
                if node_state[node_id]['ip'] != addr[0]:
                    node_state[node_id]['ip'] = addr[0]

        except Exception as e:
            # print(f"[ERRO UDP] {e}")
            pass

def display_monitor():
    """Mostra o estado da rede e a árvore de multicast."""
    while True:
        time.sleep(2) # Refresh rate

        with state_lock:
            if not node_state:
                continue

            # Limpar tela
            print("\033[2J\033[H", end="")
            
            now = time.time()
            print("=" * 100)
            print(f"MONITORIZAÇÃO OVERLAY NETWORK - {datetime.now().strftime('%H:%M:%S')}")
            print("=" * 100)

            # 1. TABELA DE NÓS (removida coluna IP do nó; mostramos IPs dos vizinhos e custos)
            print(f"{'ID':<12} {'STATUS':<8} {'VIZINHOS (IP:CUSTO)'}")
            print("-" * 80)
            
            active_streamers = []
            
            for nid in sorted(node_state.keys()):
                info = node_state[nid]
                
                # Verifica estado (Alive/Dead)
                last_seen_sec = now - info['last_seen']
                alive = last_seen_sec < 10
                status = "🟢 ON" if alive else "🔴 OFF"
                
                # Formata vizinhos mostrando IPs e custos (arredondados)
                neighbors = info.get('neighbors', {})
                active_neighbors_str = []

                for n_ip, data_val in neighbors.items():
                    # Extrai custo
                    cost = 0
                    if isinstance(data_val, dict):
                        cost = data_val.get('metric', 0)
                    else:
                        try:
                            cost = float(data_val)
                        except:
                            cost = 0

                    active_neighbors_str.append(f"{n_ip}({cost:.2f})")

                neighbors_display = ", ".join(active_neighbors_str) if active_neighbors_str else "-"
                # Não mostramos mais a coluna IP do nó; alinhamento: ID | STATUS | VIZINHOS
                print(f"{nid:<12} {status:<8} {neighbors_display}")
                
                # Identifica Streamers ativos para desenhar a árvore depois
                if "STREAMER" in nid and alive:
                    active_streamers.append(nid)

            # 2. VISUALIZAÇÃO DA ÁRVORE
            print("\n" + "="*100)
            print(" ÁRVORE DE MULTICAST (Topology Visualizer)")
            print("="*100)
            
            if not active_streamers:
                print(" [!] Nenhum Streamer detetado ou ativo.")
            else:
                for s_id in active_streamers:
                    print(f"\n📺 Origem do Stream: {s_id}")
                    # A árvore começa no Streamer (s_id) e segue o fluxo do stream (s_id)
                    print_tree(s_id, s_id, "", True)

            print("\n[Ctrl+C para sair]")

# --- MAIN ---

def start_tracker():
    load_topology()
    
    # Servidor TCP (Registo)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((BIND_IP, BIND_PORT))
    except Exception as e:
        print(f"[FATAL] Erro bind TCP {BIND_PORT}: {e}")
        sys.exit(1)
    server.listen(5)
    print(f"[*] Bootstrapper TCP a correr em {BIND_IP}:{BIND_PORT}")

    # Servidor UDP (Monitorização)
    monitor_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        monitor_sock.bind((BIND_IP, MONITOR_PORT))
    except Exception as e:
        print(f"[FATAL] Erro bind UDP {MONITOR_PORT}: {e}")
        sys.exit(1)
    print(f"[*] Monitor UDP a correr em {BIND_IP}:{MONITOR_PORT}")

    # Threads
    threading.Thread(target=handle_state_update, args=(monitor_sock,), daemon=True).start()
    threading.Thread(target=display_monitor, daemon=True).start()

    # Loop Principal (Aceitar conexões TCP)
    while True:
        try:
            client, addr = server.accept()
            threading.Thread(target=handle_client, args=(client, addr), daemon=True).start()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[ERRO ACCEPT] {e}")

if __name__ == "__main__":
    try:
        start_tracker()
    except KeyboardInterrupt:
        print("\n\n[*] Tracker encerrado.")
        sys.exit(0)