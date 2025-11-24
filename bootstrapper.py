import socket
import threading
import json
import random

# Configuração do Tracker
BIND_IP = "0.0.0.0"
BIND_PORT = 6000 # Porta TCP para registo

# Lista de nós ativos: [{'id': 'R1', 'ip': '10.0.1.1'}, ...]
active_nodes = []
lock = threading.Lock()

def handle_client(client_sock, addr):
    try:
        # 1. Receber pedido de registo
        data = client_sock.recv(1024)
        if not data: return
        
        request = json.loads(data.decode('utf-8'))
        node_id = request.get('id')
        node_ip = request.get('ip') # O nó diz o seu IP (ou usamos addr[0])
        
        print(f"[TRACKER] Pedido de registo de {node_id} ({node_ip})")
        
        response_neighbors = []
        
        with lock:
            # 2. Escolher vizinhos aleatórios para ele (ex: 2 vizinhos)
            # Filtra para não devolver o próprio nó
            candidates = [n for n in active_nodes if n['id'] != node_id]
            
            if len(candidates) > 0:
                # Se houver poucos, devolve todos. Se houver muitos, escolhe 2.
                k = min(len(candidates), 2)
                selected = random.sample(candidates, k)
                response_neighbors = [n['ip'] for n in selected]
            
            # 3. Adicionar este novo nó à lista (para os próximos o encontrarem)
            # Evitar duplicados (atualizar se já existe)
            existing = next((item for item in active_nodes if item["id"] == node_id), None)
            if not existing:
                active_nodes.append({'id': node_id, 'ip': node_ip})
            else:
                existing['ip'] = node_ip # Atualiza IP se mudou
                
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