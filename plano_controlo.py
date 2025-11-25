import time
import json
import math
import heapq
import estado_global

# Frequência de anúncio LSA (em segundos)
LSA_INTERVAL = 10 
# Tempo de vida de um LSA na topologia (em segundos)
LSA_LIFETIME = 30 


# --- 1. Lógica do Protocolo LSA ---

def thread_lsa_anuncio(send_udp_func):
    """
    Thread responsável por construir e inundar o LSA (Link State Advertisement)
    com a informação local da tabela de vizinhos. 
    """
    
    # Garante que o estado global está inicializado
    if not estado_global.NODE_STATE:
        time.sleep(1) # Espera um pouco, se necessário
        
    MY_ID = estado_global.NODE_STATE.MY_ID
    
    while estado_global.NODE_STATE.running:
        time.sleep(LSA_INTERVAL)
        
        # 1. Obter estado local e incrementar sequência
        with estado_global.NODE_STATE.lock_vizinhos:
            
            # Constrói a lista de links ativos (custo < infinito)
            links_ativos = {}
            for viz_id, dados in estado_global.NODE_STATE.tabela_vizinhos.items():
                custo = dados.get('custo_link', float('inf'))
                
                # Só anunciamos links ativos e com custo definido
                if custo < float('inf'):
                    links_ativos[viz_id] = round(custo, 2)
        
        # 2. Atualizar estado local no mapa_topologia (Incrementa o Seq)
        with estado_global.NODE_STATE.lock_topologia:
            # Obtém o LSA local atual ou inicializa se não existir
            lsa_local = estado_global.NODE_STATE.mapa_topologia.get(MY_ID, {'seq': 0, 'timestamp': time.time(), 'links': {}})
            
            # Incrementa o número de sequência
            lsa_local['seq'] += 1
            lsa_local['timestamp'] = time.time()
            lsa_local['links'] = links_ativos
            estado_global.NODE_STATE.mapa_topologia[MY_ID] = lsa_local

            # Constrói o pacote LSA para inundação
            lsa_packet = {
                'type': 'LSA',
                'source': MY_ID,
                'seq': lsa_local['seq'],
                'timestamp': lsa_local['timestamp'],
                'links': links_ativos
            }

        # 3. Inundar o LSA para todos os vizinhos
        lsa_bytes = json.dumps(lsa_packet).encode('utf-8')
        
        with estado_global.NODE_STATE.lock_vizinhos:
            vizinhos = estado_global.NODE_STATE.tabela_vizinhos.copy()
            
        for viz_id, dados in vizinhos.items():
            ip, port = dados['addr']
            send_udp_func(ip, port, lsa_bytes)
            
        print(f"[{MY_ID}] 📢 LSA seq={lsa_packet['seq']} anunciado a {len(links_ativos)} vizinhos.")
        
        # 4. Limpeza da Topologia
        limpar_topologia()

def limpar_topologia():
    """Remove entradas de nós que não anunciam LSA há mais tempo que o LSA_LIFETIME."""
    MY_ID = estado_global.NODE_STATE.MY_ID
    
    with estado_global.NODE_STATE.lock_topologia:
        topologia_copia = list(estado_global.NODE_STATE.mapa_topologia.keys())
        tempo_atual = time.time()
        
        for node_id in topologia_copia:
            if node_id == MY_ID:
                continue
            
            lsa_data = estado_global.NODE_STATE.mapa_topologia.get(node_id)
            if lsa_data and (tempo_atual - lsa_data.get('timestamp', 0)) > LSA_LIFETIME:
                print(f"[{MY_ID}] LSA de {node_id} expirou e foi removido.")
                del estado_global.NODE_STATE.mapa_topologia[node_id]

def process_lsa(lsa_dict, sender_addr, send_udp_func):
    """
    Processa um LSA recebido: verifica a validade (sequência), 
    atualiza a topologia local e inunda (flooda) o LSA para os vizinhos.
    """
    source_id = lsa_dict['source']
    seq_num = lsa_dict['seq']
    MY_ID = estado_global.NODE_STATE.MY_ID
    
    # Não processar LSA próprio (se vier por loopback)
    if source_id == MY_ID:
        return
        
    is_new = False
    with estado_global.NODE_STATE.lock_topologia:
        # Usamos mapa_topologia
        current_lsa = estado_global.NODE_STATE.mapa_topologia.get(source_id)
        
        # 1. Verifica se o LSA é novo ou mais recente
        if current_lsa is None or seq_num > current_lsa.get('seq', 0):
            
            # Atualiza a topologia
            estado_global.NODE_STATE.mapa_topologia[source_id] = {
                'seq': seq_num,
                'timestamp': lsa_dict.get('timestamp', time.time()), # Usa o timestamp se existir, ou o tempo atual
                'links': lsa_dict['links']
            }
            is_new = True
            print(f"[{MY_ID}] 🗺️ LSA de {source_id} (Seq: {seq_num}) recebido e atualizado.")

            
    # 2. Se for novo/mais recente, inunda para todos os vizinhos (exceto o que enviou)
    if is_new:
        lsa_bytes = json.dumps(lsa_dict).encode('utf-8')
        
        vizinho_origem_id = None
        # Encontra o ID do vizinho que enviou o pacote (para evitar loop de inundação)
        with estado_global.NODE_STATE.lock_vizinhos:
            # Tenta encontrar o nome do vizinho pela morada (addr)
            for nome, dados in estado_global.NODE_STATE.tabela_vizinhos.items():
                if dados.get('addr') == sender_addr:
                    vizinho_origem_id = nome
                    break
            
            vizinhos = estado_global.NODE_STATE.tabela_vizinhos.copy()
        
        n_flooded = 0
        for viz_id, dados in vizinhos.items():
            # Não enviar de volta para o nó que enviou (Split Horizon/Pruning)
            if viz_id != vizinho_origem_id:
                ip, port = dados['addr']
                send_udp_func(ip, port, lsa_bytes)
                n_flooded += 1
                
        if n_flooded > 0:
            print(f"[{MY_ID}] 🔄 LSA de {source_id} inundado a {n_flooded} vizinhos.")

# --- 2. Algoritmo A* (Dijkstra) ---

def calculate_a_star_route(source_id, destination_id):
    """
    Calcula a rota de menor custo (Dijkstra) entre 'source_id' (Streamer) e 'destination_id' (Nó Local)
    para encontrar o próximo salto (upstream) do nó local em direção à fonte.
    
    Retorna: (proximo_salto, custo_total_para_source)
    """
    
    start_node = source_id 
    target_node = destination_id
    
    with estado_global.NODE_STATE.lock_topologia:
        topologia = estado_global.NODE_STATE.mapa_topologia.copy()
    
    # Verifica se os nós existem
    if start_node not in topologia or target_node not in topologia:
        # Se um dos nós não estiver no mapa, não há rota.
        return None, float('inf')
        
    # [Image of Dijkstra's algorithm calculating the shortest path in a graph]
    
    # Inicialização do Dijkstra
    distances = {node: float('inf') for node in topologia.keys()}
    distances[start_node] = 0
    predecessors = {node: None for node in topologia.keys()}
    pq = [(0, start_node)] # (custo, nó)
    
    # Adicionar o nó local (target_node) à lista de nós se não estiver no mapa (caso extremo)
    if target_node not in distances:
        distances[target_node] = float('inf')
        predecessors[target_node] = None
        
    while pq:
        current_distance, current_node = heapq.heappop(pq)
        
        if current_distance > distances[current_node]:
            continue
            
        if current_node == target_node:
            break
            
        # Garante que o LSA do nó atual existe antes de iterar os links
        if current_node in topologia:
            # Iterar sobre os links anunciados pelo nó current_node
            for neighbor_node, weight in topologia[current_node]['links'].items():
                
                # Certifica-se que o nó vizinho também é um nó conhecido no mapa (embora o peso seja de um LSA válido)
                if neighbor_node not in distances:
                     distances[neighbor_node] = float('inf')
                     predecessors[neighbor_node] = None

                # Lógica central do Dijkstra
                new_distance = current_distance + weight
                
                if new_distance < distances[neighbor_node]:
                    distances[neighbor_node] = new_distance
                    predecessors[neighbor_node] = current_node
                    heapq.heappush(pq, (new_distance, neighbor_node))

    
    custo_total = distances.get(target_node, float('inf'))
    
    if custo_total == float('inf'):
        # Não há rota
        return None, float('inf') 

    # 2. Reconstruir a rota e encontrar o próximo salto (Upstream)
    
    if target_node == start_node:
        # O nó local é a própria fonte (Streamer)
        return 'SELF', 0.0
    
    # Seguir o caminho inverso até encontrar o predecessor do nó local (target_node)
    path = []
    step = target_node
    while step in predecessors and predecessors[step] is not None:
        path.append(step)
        step = predecessors[step]
    
    # O predecessor do nó local é o próximo salto
    if len(path) > 0:
        # Recupera o predecessor (o vizinho que está no caminho mais curto para a fonte)
        proximo_salto = predecessors[target_node]
        
        if proximo_salto is None or proximo_salto == target_node:
             # Isso só deve acontecer se a rota for direta (target_node == start_node), já tratado acima.
            return None, float('inf')
            
        return proximo_salto, custo_total
    else:
        # Não conseguiu reconstruir o caminho (ex: target_node não tem predecessor)
        return None, float('inf')
