import os
import socket
import json
import threading
import sys
from collections import defaultdict


PORT = 5555 # Porta do Bootstrapper
DEFAULT_OVERLAY_PORT = 6000 # Porta padrão para nós que ainda não se registaram
CONF_FILE = 'bootstrp_conf.json'

ACTIVE_NODES = {} # {node_id: {'ip': ip, 'port': port}} -> Mapa DINÂMICO de nós ativos
NODE_NEIGHBORS = {} # {node_id: [neighbor_ip1, neighbor_ip2, ...]} -> Mapa ESTÁTICO de vizinhos


def load_config():
    """
    Carrega a configuração da rede.
    Cria apenas o mapa estático ID -> IPs Vizinhos.
    """
    global NODE_NEIGHBORS
    
    try:
        if not os.path.exists(CONF_FILE):
             print(f"[ERROR] O ficheiro de configuração '{CONF_FILE}' não foi encontrado.")
             return False

        with open(CONF_FILE, 'r') as f: # Abre o ficheiro 'bootstrp_conf'
            config_data = json.load(f) # Carrega o JSON do ficheiro
        
        nodes = config_data.get("nodes", [])
        
        for node in nodes:
            node_id = node["id"]
            neighbor_ips = node.get("neighbors", [])
            
            # Mapeia ID -> IPs dos Vizinhos (Única informação estática fiável)
            NODE_NEIGHBORS[node_id] = neighbor_ips

        # *** A lógica de criação do IP_TO_ID foi removida ***
        
        print(f"[CONFIG] Configuração estática carregada para {len(NODE_NEIGHBORS)} nós.")
        return True

    except json.JSONDecodeError:
        print(f"[ERROR] O ficheiro '{CONF_FILE}' contém JSON inválido.")
        return False
    except Exception as e:
        print(f"[ERROR] Erro ao carregar ou processar o ficheiro de configuração: {e}")
        return False
    

def handle_client(client_socket, client_address):
    """
    Lida com a conexão TCP de um nó overlay que se está a registar.
    
    O nó que se regista DEVE enviar o seu ID, IP (para onde o podem contactar) e PORTA.
    """
    try:
        # Recebe a mensagem de registo do nó
        data = client_socket.recv(4096)
        if not data: return

        # Assume que o nó envia {'id': 'R3', 'ip': '10.0.10.1', 'port': 6000}
        reg_info = json.loads(data.decode('utf-8'))
        
        node_id = reg_info.get('id')
        node_ip = reg_info.get('ip', client_address[0]) # Usa o IP do socket como fallback
        node_port = reg_info.get('port', DEFAULT_OVERLAY_PORT)
        
        if not node_id:
            print("[ERROR] Registo inválido: falta o ID do nó.")
            return

        # 1. Registo do Nó (Aprende o IP/Porta do nó que se regista)
        ACTIVE_NODES[node_id] = {'ip': node_ip, 'port': node_port}
        print(f"[BOOTSTRAP] Nó registrado: {node_id} @ {node_ip}:{node_port}")

        # 2. Obtém os IPs dos vizinhos (Lista estática do JSON)
        neighbor_ips = NODE_NEIGHBORS.get(node_id, []) 
        # 3. Constrói a lista de vizinhos a devolver
        neighbors_to_send = []
        for n_ip in neighbor_ips:
            # Assumimos que o IP listado no JSON (n_ip) é o IP de contacto do vizinho.
            
            neighbor_is_active = False
            found_neighbor_id = n_ip # ID padrão: o IP (Se não for encontrado)
            n_data = {'ip': n_ip, 'port': DEFAULT_OVERLAY_PORT}
            
            # Pesquisa por correspondência de IP em todos os nós ativos
            for active_id, active_info in ACTIVE_NODES.items():
                if active_info['ip'] == n_ip:
                    # Caso A: Vizinho encontrado e está ativo
                    found_neighbor_id = active_id
                    n_data = active_info
                    neighbor_is_active = True
                    break
            
            if neighbor_is_active:
                # Se o vizinho está ativo, devolvemos os dados de registo
                neighbors_to_send.append({
                    'id': found_neighbor_id,
                    'ip': n_data['ip'],
                    'port': n_data['port'] 
                })
            else:
                # Caso B: Vizinho inativo. Não podemos saber o seu ID real, 
                # mas devolvemos o IP de contacto (n_ip) e o ID (assumido) é o próprio IP.
                neighbors_to_send.append({
                    'id': n_ip, # O nó que se regista terá de tentar conectar-se com este IP.
                    'ip': n_ip, 
                    'port': DEFAULT_OVERLAY_PORT 
                })
        
        # 4. Envia a resposta TCP ao nó que se está a registar
        response = {'status': 'OK', 'neighbors': neighbors_to_send}
        client_socket.sendall(json.dumps(response).encode('utf-8'))
        
    except json.JSONDecodeError:
        response = {'status': 'ERROR', 'message': 'JSON inválido.'}
        client_socket.sendall(json.dumps(response).encode('utf-8'))
    except Exception as e:
        print(f"[ERROR] Erro no manuseamento do cliente: {e}")
    finally:
        client_socket.close()




#iniciar o servidor  eiode

def start_server():
    """Inicia o servidor TCP de bootstrap."""
    if not load_config(): 
        print("Impossível carregar a configuração. Servidor não iniciado.")
        return
        
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Liga a 0.0.0.0 para aceitar conexões em qualquer interface
        server_socket.bind(("0.0.0.0", PORT)) 
        server_socket.listen()
        print(f"Bootstrapper iniciado e a escutar na porta {PORT}...")

        while True:
            client_socket, client_address = server_socket.accept()
            # Inicia uma thread para lidar com cada novo nó
            threading.Thread(target=handle_client, args=(client_socket, client_address)).start()
    except Exception as e:
        print(f"Erro fatal no servidor: {e}")
    finally:
        server_socket.close()

if __name__ == '__main__':
    start_server()
