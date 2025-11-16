import socket
import json
import threading
import sys
import time
from collections import deque

# --- Configuração ---
# CORRIGIDO: IP de R3 (Bootstrap) na interface eth1 é 10.0.10.1
BOOTSTRAP_IP = '10.0.10.1' 
BOOTSTRAP_PORT = 5555       # Porta TCP para o bootstrap
NODE_UDP_PORT = 6000        # Porta UDP para comunicação P2P
# O ID do nó deve ser passado como argumento (Ex: python node.py O5)
MY_ID = sys.argv[1] if len(sys.argv) > 1 else 'O_UNKWN' 
MY_IP = '0.0.0.0'           # Escuta em todas as interfaces
LSA_SEQUENCE_NUM = 0        # Contador de sequência para LSA

# --- Estruturas Globais do Nó Overlay ---
class OverlayNode:
    """Armazena o estado e dados do nó overlay."""
    def __init__(self, node_id, udp_port):
        self.id = node_id
        self.udp_port = udp_port
        # Dicionário de vizinhos: { 'O2': {'ip': '10.0.1.1', 'port': 6000, 'link_cost': 1.0} }
        self.neighbors = {} 
        # Tabela de adjacência global (para A*): { 'O7': {'O2': 1.5, 'C2': 2.0}, ... }
        self.adjacency_list = {} 
        # Tabela de rotas UNICAST: { 'S1_C1': { 'next_hop': 'O5', 'cost': 5.0, 'path': [...] } }
        self.routing_table = {} 
        print(f"Nó Overlay {self.id} iniciado.")

NODE_STATE = OverlayNode(MY_ID, NODE_UDP_PORT)

# --- Algoritmo A* (Melhor Caminho com Métrica de Custo/Latência) ---

def heuristic(node_a_id, node_b_id):
    """
    Heurística (h(n)). Usamos 0 para garantir que o caminho de menor custo real seja encontrado (Dijkstra).
    """
    return 0

def reconstruct_path(came_from, current_id, g_score):
    """Reconstrói o caminho e calcula o custo total."""
    path = deque()
    cost = g_score.get(current_id, float('inf'))
    
    while current_id is not None:
        path.appendleft(current_id)
        current_id = came_from.get(current_id)
    
    return list(path), cost

def find_best_path_a_star(start_node_id, goal_node_id):
    """
    Implementa A* para encontrar o caminho de menor custo (maior BW/menor latência).
    """
    
    # Validação mínima da topologia para A*
    known_nodes = set(NODE_STATE.adjacency_list.keys())
    if start_node_id not in known_nodes or goal_node_id not in known_nodes:
        # Tenta incluir vizinhos conhecidos se ainda não estiverem na lista de adjacência como keys
        if start_node_id == MY_ID and not NODE_STATE.adjacency_list.get(MY_ID):
             # Se o próprio nó não tem adjacências (ex: nó acabado de ligar)
             pass
        else:
             print(f"[A*] Aviso: Nó de partida/destino '{start_node_id}' ou '{goal_node_id}' não na topologia global. ")
             return None, float('inf')

    open_set = {start_node_id}
    came_from = {start_node_id: None}
    
    # Inicializa scores. Usa todos os nós que são chaves na adjacency_list.
    all_nodes = NODE_STATE.adjacency_list.keys()
    g_score = {node_id: float('inf') for node_id in all_nodes}
    g_score[start_node_id] = 0
    f_score = {node_id: float('inf') for node_id in all_nodes}
    f_score[start_node_id] = heuristic(start_node_id, goal_node_id)

    while open_set:
        # Encontrar o nó com o menor f_score
        current_id = min(open_set, key=lambda node: f_score.get(node, float('inf')))
        
        if current_id == goal_node_id:
            return reconstruct_path(came_from, current_id, g_score)

        open_set.remove(current_id)

        neighbors = NODE_STATE.adjacency_list.get(current_id, {})
        
        for neighbor_id, link_cost in neighbors.items():
            # Verifica se o vizinho é um nó conhecido na topologia
            if neighbor_id not in g_score:
                continue

            tentative_g_score = g_score[current_id] + link_cost

            if tentative_g_score < g_score.get(neighbor_id, float('inf')):
                # Encontrado um caminho melhor
                came_from[neighbor_id] = current_id
                g_score[neighbor_id] = tentative_g_score
                f_score[neighbor_id] = tentative_g_score + heuristic(neighbor_id, goal_node_id)
                
                if neighbor_id not in open_set:
                    open_set.add(neighbor_id)
    
    return None, float('inf')

# --- Funções Auxiliares de Comunicação ---

def pack_message(type, payload):
    """Empacota a mensagem P2P (UDP) em formato JSON."""
    return json.dumps({'type': type, 'sender': MY_ID, 'payload': payload}).encode('utf-8')

def send_udp_message(ip, port, message_bytes):
    """Envia uma mensagem UDP."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(message_bytes, (ip, port))
        sock.close()
    except Exception as e:
        print(f"[UDP] Erro ao enviar mensagem para {ip}:{port}: {e}")

# --- Conexão de Bootstrap (TCP) ---

def connect_bootstrap():
    """Conecta ao R3 via TCP para obter a lista inicial de vizinhos (primeiro nó)."""
    try:
        print(f"[TCP] Conectando ao bootstrap ({BOOTSTRAP_IP}:{BOOTSTRAP_PORT})...")
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_socket.connect((BOOTSTRAP_IP, BOOTSTRAP_PORT))
        
        # Envia ID, porta e o IP real do nó para que o bootstrap possa responder
        registration_msg = json.dumps({'id': MY_ID, 'port': NODE_UDP_PORT, 'ip': get_my_unicast_ip()}).encode('utf-8')
        client_socket.sendall(registration_msg)
        
        response_data = client_socket.recv(4096).decode('utf-8')
        response = json.loads(response_data)
        client_socket.close()

        if response.get('neighbors'):
            print(f"[TCP] Vizinhos iniciais recebidos: {[n['id'] for n in response['neighbors']]}")
            
            for n in response['neighbors']:
                cost = n.get('link_cost', 1.0) # Assumimos custo inicial 1.0
                
                NODE_STATE.neighbors[n['id']] = {'ip': n['ip'], 'port': n['port'], 'link_cost': cost}
                
                # Inicializa a adjacência local
                if MY_ID not in NODE_STATE.adjacency_list:
                    NODE_STATE.adjacency_list[MY_ID] = {}
                NODE_STATE.adjacency_list[MY_ID][n['id']] = cost
            
            # Começa o serviço P2P (UDP)
            threading.Thread(target=start_udp_service).start()
        
    except socket.error as e:
        print(f"[TCP] Erro de conexão ao bootstrap: {e}. Tentando novamente em 5s...")
        time.sleep(5)
        connect_bootstrap()
    except Exception as e:
        print(f"[TCP] Erro: {e}")

def get_my_unicast_ip():
    """Retorna o endereço IP Unicast da máquina no CORE."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('1.1.1.1', 1))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except:
        return '127.0.0.1' 

# --- Serviço P2P (UDP) ---

def process_p2p_message(message, addr):
    """Processa mensagens P2P recebidas via UDP."""
    global LSA_SEQUENCE_NUM
    
    try:
        msg = json.loads(message.decode('utf-8'))
        msg_type = msg.get('type')
        sender = msg.get('sender')
        payload = msg.get('payload')
        
        # 1. Atualização da Topologia (LINK STATE ADVERTISEMENT - LSA)
        if msg_type == 'LSA':
            source_node = payload.get('source')
            new_links = payload.get('links')
            seq_num = payload.get('seq')

            # Implementação de Link State muito simples: aceita e propaga.
            # Idealmente, haveria um check de seq_num para evitar pacotes antigos.
            
            # Atualiza a lista de adjacência global
            NODE_STATE.adjacency_list[source_node] = new_links
            
            # Reenvia LSA para vizinhos (Flood Control)
            threading.Thread(target=flood_lsa, args=(msg, sender)).start()
            
        # 2. Dados de Stream (STREAM_DATA)
        elif msg_type == 'STREAM_DATA':
            dest_id = payload.get('dest_id')
            forward_stream_data(dest_id, payload)
        
        # 3. Pedido de Ativação de Rota (CLIENT/SERVER)
        elif msg_type == 'ROUTE_REQUEST':
            # Este é um esquema de roteamento reativo (similar ao DSR/AODV RREQ)
            flux_id = payload.get('flux_id')
            source_id = payload.get('source_id')
            dest_id = payload.get('dest_id')
            
            if MY_ID.startswith('STREAMER'): # Assumindo que o servidor tem prefixo STREAMER
                # O SERVIDOR (STREAMER) corre o A* para o destino final
                path, cost = find_best_path_a_star(source_id, dest_id)
                if path:
                    # O servidor deve enviar a resposta (ROUTE_RESPONSE)
                    response_payload = {'flux_id': flux_id, 'path': path, 'cost': cost}
                    send_route_response(dest_id, response_payload)
            else:
                # Nós intermediários (O-Nodes) reencaminham o pedido para o servidor
                forward_route_request(msg)
        
        # 4. Resposta de Rota (SERVER)
        elif msg_type == 'ROUTE_RESPONSE':
            # Recebe a rota completa calculada pelo servidor e armazena o próximo salto.
            flux_id = payload.get('flux_id')
            path = payload.get('path')
            cost = payload.get('cost')
            
            # Atualiza a tabela de rotas local com a info do A* (calculado em S1)
            update_routing_table_from_path(flux_id, path, cost)
            
            # Reencaminha a resposta para o próximo nó no caminho de volta ao cliente (simplificação)
            forward_route_response(msg, path, sender)

    except json.JSONDecodeError:
        print(f"[UDP] Erro ao decodificar a mensagem JSON de {addr}")
    except Exception as e:
        print(f"[UDP] Erro ao processar mensagem: {e}")

def start_udp_service():
    """Inicia o servidor UDP para comunicação P2P."""
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        server_socket.bind(('', NODE_UDP_PORT))
        print(f"[UDP] Nó overlay {MY_ID} escutando em {MY_IP}:{NODE_UDP_PORT}")
        
        # Inicia o processo de Link State (envio periódico e flood)
        threading.Thread(target=link_state_management).start()

        while True:
            data, addr = server_socket.recvfrom(4096)
            threading.Thread(target=process_p2p_message, args=(data, addr)).start()
            
    except Exception as e:
        print(f"[UDP] Erro fatal no servidor: {e}")
    finally:
        server_socket.close()

def link_state_management():
    """Envia LSA periodicamente para manter a topologia atualizada."""
    global LSA_SEQUENCE_NUM
    while True:
        LSA_SEQUENCE_NUM += 1
        payload = {
            'source': MY_ID, 
            'links': NODE_STATE.adjacency_list.get(MY_ID, {}),
            'seq': LSA_SEQUENCE_NUM
        }
        lsa_msg = pack_message('LSA', payload)
        
        for n_id, n_data in NODE_STATE.neighbors.items():
            send_udp_message(n_data['ip'], n_data['port'], lsa_msg)
        
        # Envio a cada 10 segundos
        time.sleep(10) 

def flood_lsa(lsa_msg_dict, incoming_sender_id):
    """Reencaminha o LSA para todos, exceto o remetente original."""
    lsa_msg_bytes = json.dumps(lsa_msg_dict).encode('utf-8')
    
    for n_id, n_data in NODE_STATE.neighbors.items():
        if n_id != incoming_sender_id:
            send_udp_message(n_data['ip'], n_data['port'], lsa_msg_bytes)

def forward_route_request(msg):
    """Reencaminha o ROUTE_REQUEST (Flood simples ou Unicast para S1, dependendo da info)."""
    # Simplificação: Flooda o RREQ até S1
    flux_id = msg['payload']['flux_id']
    sender = msg['sender']
    print(f"[ROUTE] Reencaminhando ROUTE_REQUEST para {flux_id}.")
    
    msg_bytes = json.dumps(msg).encode('utf-8')
    
    for n_id, n_data in NODE_STATE.neighbors.items():
        if n_id != sender:
            send_udp_message(n_data['ip'], n_data['port'], msg_bytes)

def send_route_response(dest_id, payload):
    """Envia a ROUTE_RESPONSE do servidor (S1) de volta ao cliente (dest_id)."""
    # Para enviar a resposta de volta, o servidor precisa saber o caminho de S1->dest_id
    # e usar a ordem inversa do caminho calculado pelo A* (Path Reversal)
    path = payload['path']
    flux_id = payload['flux_id']
    
    if MY_ID != path[0]:
        print("[ROUTE] Erro: A resposta de rota deve começar no nó inicial do path.")
        return
        
    # Encontra o próximo salto (nó seguinte no caminho)
    if len(path) > 1:
        next_hop_id = path[1]
        n_data = NODE_STATE.neighbors.get(next_hop_id)
        if n_data:
            response_msg = pack_message('ROUTE_RESPONSE', payload)
            send_udp_message(n_data['ip'], n_data['port'], response_msg)
            print(f"[ROUTE] Enviando ROUTE_RESPONSE para {next_hop_id} no caminho de volta.")

def forward_route_response(msg, path, incoming_sender_id):
    """Reencaminha a ROUTE_RESPONSE no caminho de volta ao cliente."""
    # O nó deve estar no path. A resposta deve ir para o nó anterior no path.
    if MY_ID in path:
        my_index = path.index(MY_ID)
        # O destino é o nó anterior no caminho
        dest_index = my_index - 1
        
        if dest_index >= 0:
            next_hop_id = path[dest_index]
            n_data = NODE_STATE.neighbors.get(next_hop_id)
            
            if n_data and next_hop_id != incoming_sender_id:
                forward_msg = json.dumps(msg).encode('utf-8')
                send_udp_message(n_data['ip'], n_data['port'], forward_msg)
                print(f"[ROUTE] Reencaminhando ROUTE_RESPONSE para {next_hop_id}.")
            elif n_data and next_hop_id == incoming_sender_id:
                 # Recebeu de um nó que está atrás no path. Sinal de loop ou erro. Ignorar.
                 pass
            
def update_routing_table_from_path(flux_id, path, cost):
    """
    Atualiza a tabela de rotas local, extraindo o próximo salto do caminho A*.
    """
    if MY_ID in path:
        my_index = path.index(MY_ID)
        next_hop = path[my_index + 1] if my_index + 1 < len(path) else None
        
        NODE_STATE.routing_table[flux_id] = {
            'next_hop': next_hop, 
            'cost': cost,
            'path': path # Armazena o path completo (para o path reversal do RREP, se necessário)
        }
        print(f"[ROUTE] Tabela atualizada para {flux_id}. Próximo salto: {next_hop}")
    elif MY_ID.startswith('C'): # Se for o cliente final, não precisa de next_hop
         NODE_STATE.routing_table[flux_id] = {'next_hop': None, 'cost': cost, 'path': path}
    
def forward_stream_data(dest_id, payload):
    """Reencaminha pacotes de stream multimédia usando Unicast."""
    flux_id = f"S1_{dest_id}"
    route_info = NODE_STATE.routing_table.get(flux_id)

    if MY_ID == dest_id:
        print(f"[STREAM] Nó cliente {MY_ID} recebeu pacote {payload.get('seq')}. (Destino final)")
        return

    # Se a rota não existir, envia um ROUTE_REQUEST (RREQ)
    if not route_info or not route_info['next_hop']:
        print(f"[STREAM] Rota para {dest_id} não encontrada. Enviando ROUTE_REQUEST...")
        if MY_ID.startswith('C'):
             rreq_payload = {'flux_id': flux_id, 'source_id': 'STREAMER1', 'dest_id': MY_ID} # Assumindo STREAMER1
             rreq_msg = pack_message('ROUTE_REQUEST', rreq_payload)
             # Flooda o RREQ para vizinhos
             for n_id, n_data in NODE_STATE.neighbors.items():
                 send_udp_message(n_data['ip'], n_data['port'], rreq_msg)
        return
        
    if route_info and route_info['next_hop']:
        next_hop_id = route_info['next_hop']
        n_data = NODE_STATE.neighbors.get(next_hop_id)
        
        if n_data:
            forward_msg = pack_message('STREAM_DATA', payload)
            send_udp_message(n_data['ip'], n_data['port'], forward_msg)
            print(f"[STREAM] Reencaminhando pacote {payload.get('seq')} para {next_hop_id}.")
        else:
            print(f"[STREAM] Erro: Next hop {next_hop_id} desconhecido (vizinho perdido?).")
    else:
        print(f"[STREAM] Não foi possível encontrar rota para {dest_id}.")
# --- Funções Específicas para o Bootstrap (R3) ---

def initialize_r3_neighbors():
    """
    Inicializa os vizinhos do R3 localmente sem usar a conexão TCP.
    
    NOTA: Estime os IDs e IPs dos vizinhos de R3 com base na sua topologia.
    Assumindo que R3 tem R7 e R1 como vizinhos diretos (apenas para exemplo).
    """
    R3_LOCAL_NEIGHBORS = {
        'R7': {'ip': '10.0.10.2', 'port': 6000, 'link_cost': 1.0}, # Exemplo do IP do R7
        'R1': {'ip': '10.0.8.2', 'port': 6000, 'link_cost': 1.0},  # Exemplo do IP do R1
        # Adicione outros vizinhos de R3 aqui.
    }
    
    print("[R3] Inicializando vizinhos locais (Bootstrap bypass).")
    
    for n_id, n_data in R3_LOCAL_NEIGHBORS.items():
        cost = n_data['link_cost']
        
        NODE_STATE.neighbors[n_id] = n_data
        
        # Inicializa a adjacência local
        if MY_ID not in NODE_STATE.adjacency_list:
            NODE_STATE.adjacency_list[MY_ID] = {}
        NODE_STATE.adjacency_list[MY_ID][n_id] = cost


# --- Execução Principal (Alteração Necessária) ---

if __name__ == "__main__":
    if MY_ID == 'O_UNKWN':
        print("ERRO: É necessário fornecer o ID do nó como argumento (Ex: python node.py O5)")
        sys.exit(1)

    # 1. Lógica de Inicialização Condicional
    if MY_ID == 'R3': # Se for o nó Bootstrap
        print("[R3] Modo Bootstrap: Inicialização local e arranque do serviço P2P.")
        initialize_r3_neighbors() # Popula o estado local
        # O R3 deve iniciar o serviço UDP diretamente
        threading.Thread(target=start_udp_service).start()
    else:
        # Outros nós (R1, C6, etc.) executam o processo normal de cliente
        # 2. Inicia a conexão de bootstrap (TCP)
        connect_bootstrap()
