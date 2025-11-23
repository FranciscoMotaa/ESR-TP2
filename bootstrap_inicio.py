import os
import socket
import json
import threading

PORT = 5555
DEFAULT_OVERLAY_PORT = 6000 # Mantém a mesma porta que usas nos nós!
CONF_FILE = 'bootstrap_conf.json'

# Mapeamento Global Estático: ID -> Dados do Nó (IP, Vizinhos)
# Exemplo: { "R1": {"ip": "10.0.17.1", "neighbors": ["R3", "R4"]} }
TOPOLOGIA = {}

def load_config():
    """
    Carrega o JSON e indexa TUDO por ID para acesso rápido.
    """
    global TOPOLOGIA
    try:
        if not os.path.exists(CONF_FILE):
             print(f"[ERRO] Ficheiro '{CONF_FILE}' não encontrado.")
             return False

        with open(CONF_FILE, 'r') as f:
            data = json.load(f)
        
        # Converte a lista num Dicionário para pesquisas rápidas
        for node in data.get("nodes", []):
            TOPOLOGIA[node["id"]] = {
                "ip": node["ip"],
                "neighbors": node["neighbors"]
            }
            
        print(f"[CONFIG] Topologia carregada: {len(TOPOLOGIA)} nós conhecidos.")
        return True
    except Exception as e:
        print(f"[ERRO] Falha ao carregar JSON: {e}")
        return False

def handle_client(conn, addr):
    """
    Responde ao nó com a lista de vizinhos baseada no JSON estático.
    Não precisamos de saber se o vizinho está 'ativo' ou não. O nó que tente contactá-lo.
    """
    try:
        # Recebe pedido (ex: {'id': 'C2', ...})
        data = conn.recv(4096)
        if not data: return
        
        msg = json.loads(data.decode())
        node_id = msg.get('id')
        
        print(f"[REGISTO] Nó '{node_id}' conectou-se de {addr}")

        response_neighbors = []

        # 1. Verificar se conhecemos este nó na topologia
        if node_id in TOPOLOGIA:
            # 2. Obter a lista de IDs dos vizinhos (ex: ["R9"])
            lista_vizinhos_ids = TOPOLOGIA[node_id]["neighbors"]
            
            # 3. Traduzir IDs para IPs usando a TOPOLOGIA carregada
            for viz_id in lista_vizinhos_ids:
                if viz_id in TOPOLOGIA:
                    viz_dados = TOPOLOGIA[viz_id]
                    response_neighbors.append({
                        "id": viz_id,
                        "ip": viz_dados["ip"], # O IP correto que está no JSON
                        "port": DEFAULT_OVERLAY_PORT # Assume porta fixa
                    })
                else:
                    print(f"[AVISO] Vizinho '{viz_id}' referenciado por '{node_id}' não existe no JSON!")

            # Envia resposta OK
            msg_resp = {'status': 'OK', 'neighbors': response_neighbors}
        else:
            # Nó desconhecido no JSON
            print(f"[ERRO] Nó '{node_id}' não consta na configuração estática.")
            msg_resp = {'status': 'ERRO', 'neighbors': []}

        conn.sendall(json.dumps(msg_resp).encode('utf-8'))

    except Exception as e:
        print(f"[ERRO] Thread cliente: {e}")
    finally:
        conn.close()

def start_server():
    if not load_config(): return
        
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) # Importante para reiniciar rápido sem erro "Address already in use"
    
    try:
        server.bind(("0.0.0.0", PORT))
        server.listen(5)
        print(f"Bootstrapper ON na porta {PORT}...")

        while True:
            client, addr = server.accept()
            threading.Thread(target=handle_client, args=(client, addr)).start()
    except Exception as e:
        print(f"[FATAL] {e}")
    finally:
        server.close()

if __name__ == '__main__':
    start_server()