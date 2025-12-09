import socket
import threading
import json
import sys
import time
from datetime import datetime
from collections import defaultdict

# Configuração do Tracker
BIND_IP = "0.0.0.0"
BIND_PORT = 6000
MONITOR_PORT = 6001  # Porta UDP para receber updates de estado
TOPOLOGY_FILE = "bootstrap_conf.json"
static_topology = {}

# Estruturas de dados para monitorização
node_state = {}  # {node_id: {'ip': ..., 'last_seen': ..., 'neighbors': {...}, 'routing_table': {...}, 'streams': [...]}}
route_history = {}  # Histórico de mudanças de rotas: {node_id: {dest_id: [(timestamp, next_hop, cost), ...]}}
neighbor_discovery = {}  # Rastreio de descoberta de vizinhos: {node_id: {neighbor_ip: first_seen_timestamp}}
state_lock = threading.Lock()

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

        # Registar nó no estado
        with state_lock:
            if node_id not in node_state:
                node_state[node_id] = {
                    'ip': node_ip,
                    'last_seen': time.time(),
                    'neighbors': {},
                    'routing_table': {},
                    'streams': []
                }

        response = json.dumps({"status": "OK", "neighbors": response_neighbors})
        client_sock.send(response.encode('utf-8'))

    except Exception as e:
        print(f"[ERRO] {e}")
    finally:
        client_sock.close()

def handle_state_update(sock):
    """Recebe updates de estado dos nós via UDP"""
    while True:
        try:
            data, addr = sock.recvfrom(8192)
            update = json.loads(data.decode('utf-8'))
            
            node_id = update.get('node_id')
            if not node_id:
                continue
            
            with state_lock:
                if node_id not in node_state:
                    node_state[node_id] = {
                        'ip': addr[0],
                        'last_seen': time.time(),
                        'neighbors': {},
                        'routing_table': {},
                        'streams': []
                    }
                    neighbor_discovery[node_id] = {}
                    route_history[node_id] = {}
                
                current_time = time.time()
                
                # Detectar novos vizinhos (descoberta dinâmica) - silencioso por padrão
                new_neighbors = update.get('neighbors', {})
                if node_id in neighbor_discovery:
                    for neighbor_ip in new_neighbors:
                        if neighbor_ip not in neighbor_discovery[node_id]:
                            neighbor_discovery[node_id][neighbor_ip] = current_time
                
                # Detectar mudanças de rotas
                new_routing_table = update.get('routing_table', {})
                old_routing_table = node_state[node_id].get('routing_table', {})
                
                for dest_id, new_route in new_routing_table.items():
                    new_next_hop = new_route.get('next_hop')
                    new_cost = new_route.get('cost', 0)
                    
                    # Inicializar histórico se não existir
                    if node_id not in route_history:
                        route_history[node_id] = {}
                    if dest_id not in route_history[node_id]:
                        route_history[node_id][dest_id] = []
                    
                    # Verificar se houve mudança
                    if dest_id in old_routing_table:
                        old_next_hop = old_routing_table[dest_id].get('next_hop')
                        old_cost = old_routing_table[dest_id].get('cost', 0)
                        
                        # Mudança de rota (next hop diferente) - mostrar apenas se significativo
                        if new_next_hop != old_next_hop:
                            route_history[node_id][dest_id].append((current_time, new_next_hop, new_cost))
                            cost_diff = new_cost - old_cost
                            if abs(cost_diff) > 10.0:  # Apenas mudanças >10ms
                                indicator = "↓" if cost_diff < 0 else "↑"
                                print(f"[{node_id}] {dest_id}: {old_next_hop}({old_cost:.0f}ms) → {new_next_hop}({new_cost:.0f}ms) [{indicator}{abs(cost_diff):.0f}ms]")
                        # Mesma rota mas custo mudou muito (>15ms)
                        elif abs(new_cost - old_cost) > 15.0:
                            route_history[node_id][dest_id].append((current_time, new_next_hop, new_cost))
                            cost_diff = new_cost - old_cost
                            indicator = "↓" if cost_diff < 0 else "↑"
                            print(f"[{node_id}] {dest_id} custo: {old_cost:.0f}ms → {new_cost:.0f}ms [{indicator}{abs(cost_diff):.0f}ms]")
                    else:
                        # Nova rota descoberta - apenas log inicial
                        route_history[node_id][dest_id].append((current_time, new_next_hop, new_cost))
                
                # Atualizar dados
                node_state[node_id]['last_seen'] = current_time
                node_state[node_id]['neighbors'] = new_neighbors
                node_state[node_id]['routing_table'] = new_routing_table
                node_state[node_id]['streams'] = update.get('streams', [])
                
        except Exception as e:
            print(f"[ERRO] Update: {e}")
            pass

def display_monitor():
    """Thread que mostra tabelas de estado periodicamente"""
    while True:
        time.sleep(3)  # Atualizar a cada 3 segundos
        
        with state_lock:
            if not node_state:
                continue
            
            # Limpar tela (ANSI escape code)
            print("\033[2J\033[H", end="")
            
            now = time.time()
            print("=" * 120)
            print(f"MONITORIZAÇÃO OVERLAY NETWORK - {datetime.now().strftime('%H:%M:%S')}")
            print("=" * 120)
            
            # Tabela de nós ativos (layout limpo)
            print("\nNÓS ATIVOS:")
            print(f"{'ID':<10} {'Estado':<7} {'Última Conexão':<16} {'Tempo':<8} {'Vizinhos Ativos':<40}")
            for node_id in sorted(node_state.keys()):
                info = node_state[node_id]
                last_seen_seconds = now - info['last_seen']
                status = "ALIVE" if last_seen_seconds < 10 else ("LOST" if last_seen_seconds < 30 else "DEAD")
                last_conn = datetime.fromtimestamp(info['last_seen']).strftime('%H:%M:%S')
                time_elapsed = f"{int(last_seen_seconds)}s" if last_seen_seconds < 60 else f"{int(last_seen_seconds/60)}m"
                neighbors = info.get('neighbors', {})
                # Mostrar todos vizinhos recebidos
                neighbors_str = ', '.join(sorted(neighbors.keys())) if neighbors else "-"
                print(f"{node_id:<10} {status:<7} {last_conn:<16} {time_elapsed:<8} {neighbors_str:<40}")
            
            # Tabela de rotas (layout limpo)
            print("\nTABELA DE ROTAS:")
            print(f"{'Nó':<10} {'Destino':<12} {'Próx. Salto':<16} {'Custo':<8} {'Downstream':<10}")
            for node_id in sorted(node_state.keys()):
                routing_table = node_state[node_id].get('routing_table', {})
                for dest_id, route_info in sorted(routing_table.items()):
                    next_hop = route_info.get('next_hop', 'N/A')
                    cost = route_info.get('cost', 0)
                    downstream_count = len(route_info.get('downstream', []))
                    print(f"{node_id:<10} {dest_id:<12} {next_hop:<16} {cost:>6.1f}ms {downstream_count:<10}")
            
            # Streams ativos
            print("\n┌─── STREAMS ATIVOS " + "─" * 98 + "┐")
            stream_info = defaultdict(list)
            for node_id, info in node_state.items():
                for stream in info.get('streams', []):
                    stream_info[stream].append(node_id)
            
            if stream_info:
                for stream_id, nodes in stream_info.items():
                    streamers = [n for n in nodes if 'STREAMER' in n]
                    clients = [n for n in nodes if 'C' in n]
                    routers = [n for n in nodes if n not in streamers and n not in clients]
                    
                    print(f"│ Stream: {stream_id:<30} Streamer: {','.join(streamers) if streamers else 'N/A':<20} Clients: {len(clients):<5} Routers: {len(routers):<5} │")
            else:
                print(f"│ {'Nenhum stream ativo':<117} │")
            
            print("└" + "─" * 119 + "┘")
            print("\n[Pressione Ctrl+C para parar o tracker]")

def start_tracker():
    load_topology()
    
    # Servidor TCP para bootstrap
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind((BIND_IP, BIND_PORT))
    server.listen(5)
    print(f"[*] Bootstrapper TCP a correr em {BIND_IP}:{BIND_PORT}")
    
    # Servidor UDP para monitorização
    monitor_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    monitor_sock.bind((BIND_IP, MONITOR_PORT))
    print(f"[*] Monitor UDP a correr em {BIND_IP}:{MONITOR_PORT}")
    
    # Iniciar threads
    threading.Thread(target=handle_state_update, args=(monitor_sock,), daemon=True).start()
    threading.Thread(target=display_monitor, daemon=True).start()
    
    print(f"[*] Sistema de monitorização ativo. Aguardando updates dos nós...")
    
    while True:
        client, addr = server.accept()
        threading.Thread(target=handle_client, args=(client, addr), daemon=True).start()

if __name__ == "__main__":
    try:
        start_tracker()
    except KeyboardInterrupt:
        print("\n\n[*] Tracker encerrado pelo utilizador.")
        sys.exit(0)