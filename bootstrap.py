import json
import subprocess
import sys
from collections import deque
import heapq # Biblioteca para a fila de prioridade do A*

JSON_FILE = "bootstrp_conf"
CURRENT_HOSTNAME = "R3" 

# --- Estrutura de Dados e Funções Auxiliares ---

def build_graph(data):
    """Constrói o grafo a partir dos dados JSON."""
    graph = {}
    node_map = {node['hostname']: node['id'] for node in data['topology_subset']['nodes']}
    
    # 1. Mapear IDs para Hostnames para fácil acesso
    host_map = {node['id']: node['hostname'] for node in data['topology_subset']['nodes']}
    
    # 2. Inicializar o grafo
    for node_id, hostname in host_map.items():
        graph[hostname] = {}
        
    # 3. Preencher as adjacências e custos
    for node in data['topology_subset']['nodes']:
        hostname = node['hostname']
        for iface, peer_id in node.get('interface-peer', {}).items():
            if peer_id in host_map: # É um roteador ou PC vizinho
                peer_hostname = host_map[peer_id]
                # Custo do link: 1 (hop count).
                graph[hostname][peer_hostname] = 1 
            else: # É um switch (LAN)
                # LANs são tratadas separadamente, mas para o roteamento, 
                # o switch atua como um único hop para todos os dispositivos ligados a ele.
                # Ignoramos switches aqui e lidamos com a sub-rede no cálculo final.
                pass
                
    return graph, data['topology_subset']['nodes']

def a_star_search(graph, start_node, goal_node, h):
    """Implementação do algoritmo A* (usando Dijkstra, pois h(n)=0 e custo=1)."""
    
    # Fila de prioridade: (custo_f, custo_g, nó)
    # Custo_f = g(n) + h(n). Como h(n) = 0, f(n) = g(n)
    priority_queue = [(0, 0, start_node)]
    
    # g_scores: custo real do início até o nó
    g_scores = {node: float('inf') for node in graph}
    g_scores[start_node] = 0
    
    # parent_path: para reconstruir o caminho
    parent_path = {node: None for node in graph}

    while priority_queue:
        # Pega o nó com menor custo f
        _, current_g, current_node = heapq.heappop(priority_queue)

        if current_node == goal_node:
            # Reconstruir caminho
            path = deque()
            while current_node is not None:
                path.appendleft(current_node)
                current_node = parent_path[current_node]
            return list(path)

        for neighbor, cost in graph[current_node].items():
            # Novo custo g (Distância do início até o vizinho via nó atual)
            new_g = current_g + cost
            
            if new_g < g_scores[neighbor]:
                g_scores[neighbor] = new_g
                parent_path[neighbor] = current_node
                # h(n) = 0 (heurística nula para hop-count)
                f_score = new_g + 0 
                heapq.heappush(priority_queue, (f_score, new_g, neighbor))
                
    return None # Caminho não encontrado

def get_next_hop_ip(current_node_config, target_router_hostname):
    """Encontra o IP do Next Hop para o primeiro roteador no caminho."""
    # Obter o ID do vizinho (target_router_hostname)
    target_node_id = next(node['id'] for node in current_node_config if node['hostname'] == target_router_hostname)
    
    # Encontrar a interface do nó atual ligada ao target_node_id
    for iface, peer_id in current_node_config['interface-peer'].items():
        if peer_id == target_node_id:
            # Retornar o IP do target_router_hostname na sub-rede ligada ao R3
            
            # ATENÇÃO: O JSON não contém o IP do vizinho, apenas o ID. 
            
            # Para o R3, o next hop para R2 é 10.0.9.1.
            return '10.0.9.1' 

    return None

# --- Funções do Bootstrapper ---

def execute_command(command):
    """Executa um comando de shell e imprime o resultado ou erro."""
    # ... (manter a função execute_command do exemplo anterior) ...
    try:
        # print(f"Executando comando: {command}")
        subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
        # print(f"✅ SUCESSO")
        return True
    except subprocess.CalledProcessError as e:
        print(f"ERRO ao executar '{command}': {e.stderr.strip()}")
        return False
    except FileNotFoundError:
        print(f"ERRO: Comando 'ip' não encontrado.")
        return False


def apply_a_star_routes(data):
    """Calcula e aplica rotas estáticas usando A*."""
    
    # 1. Construir o grafo e identificar o nó atual
    graph, all_nodes = build_graph(data)
    current_node_config = next(node for node in all_nodes if node['hostname'] == CURRENT_HOSTNAME)
    
    # Roteadores vizinhos diretos (next-hops conhecidos)
    # Este dicionário mapeia o hostname do vizinho para o IP que o R3 usará como next-hop
    # Isso é feito manualmente, pois o JSON não contém o IP do vizinho.
    NEXT_HOPS = {
        'R2': '10.0.9.1', # Vizinho R2 (eth0)
        'R7': '10.0.10.2', # Vizinho R7 (eth1)
        'R6': '10.0.13.1', # Vizinho R6 (eth2)
        'R10': '10.0.20.2', # Vizinho R10 (eth3, via S1) - Assumindo que R10 tem o IP .2 no 10.0.20.0/24
    }

    # Rotas a calcular: todas as sub-redes (extrair de todos os nós)
    target_networks = set()
    for node in all_nodes:
        for iface, config in node['network-config'].items():
            if iface.startswith('eth'):
                ip_with_prefix = config['ip']
                # Simplificação: assume que a rede é X.Y.Z.0/24
                network_prefix = ip_with_prefix.rsplit('.', 1)[0].rsplit('/', 1)[0] + '.0/' + ip_with_prefix.split('/')[1]
                target_networks.add(network_prefix)
    
    # 2. Iterar sobre os roteadores vizinhos para calcular rotas
    
    print(f"⭐ Aplicando rotas A* calculadas no R3 (Next-Hops: {list(NEXT_HOPS.keys())})")

    # Vamos iterar sobre todas as redes e encontrar o roteador que está mais próximo delas
    # Este é um passo complexo, o A* é executado entre R3 e cada OUTRO ROTEADOR (R1, R2, R4, R5, R6, R7, R8, R9, R10, R11)
    
    for dest_router_hostname in [h for h in graph.keys() if h != CURRENT_HOSTNAME and h.startswith('R')]:
        path = a_star_search(graph, CURRENT_HOSTNAME, dest_router_hostname, lambda n: 0)
        
        if path and len(path) > 1:
            first_hop_router = path[1]
            
            # Encontrar o Next-Hop IP no R3
            if first_hop_router in NEXT_HOPS:
                next_hop_ip = NEXT_HOPS[first_hop_router]
            else:
                # O primeiro hop é um roteador que não é um vizinho direto, 
                # o que não devia acontecer numa rede de roteadores.
                continue 

            # Identificar as sub-redes ligadas a dest_router_hostname
            dest_config = next(node for node in all_nodes if node['hostname'] == dest_router_hostname)
            
            for iface, config in dest_config['network-config'].items():
                if iface.startswith('eth'):
                    ip_with_prefix = config['ip']
                    network_prefix = ip_with_prefix.rsplit('.', 1)[0].rsplit('/', 1)[0] + '.0/' + ip_with_prefix.split('/')[1]
                    
                    if network_prefix not in target_networks:
                        continue # Rede já processada ou inválida
                        
                    # 3. Aplicar a Rota Estática Calculada
                    command = f"sudo ip route add {network_prefix} via {next_hop_ip}"
                    execute_command(command)
                    target_networks.discard(network_prefix) # Marcar como processada

    print("\n Rotas A* (baseadas em Hop Count) aplicadas. Verifique com 'ip route'.")

def main():
    """Função principal para carregar o JSON."""
    if not JSON_FILE:
        print("Erro: Ficheiro JSON não especificado.")
        sys.exit(1)
        
    try:
        with open(JSON_FILE, 'r') as f:
            data = json.load(f)
        apply_a_star_routes(data)
    except FileNotFoundError:
        print(f"Erro: O ficheiro '{JSON_FILE}' não foi encontrado.")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"Erro: O ficheiro '{JSON_FILE}' não é um JSON válido.")
        sys.exit(1)

if __name__ == "__main__":
    main()
