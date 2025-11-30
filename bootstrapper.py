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
        # aceitar também o porto no pedido
        node_ip = request.get('ip') or addr[0]
        node_port = request.get('port') or request.get('udp_port') or None
        if node_port is None:
            # se não foi enviado, tentamos obter da conexão TCP (não fiável)
            node_port = request.get('port')
        # normalizar para string ip:port quando possível
        if node_port:
            node_address = f"{node_ip}:{int(node_port)}"
        else:
            node_address = node_ip
        
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
                # devolver apenas o IP (main.py assume vizinhos como IP simples)
                response_neighbors = [n.get('ip') for n in selected]
            
            # 3. Adicionar este novo nó à lista (para os próximos o encontrarem)
            # Evitar duplicados (atualizar se já existe)
            existing = next((item for item in active_nodes if item["id"] == node_id), None)
            if not existing:
                # armazenar a informação completa (ip e address)
                entry = {'id': node_id, 'ip': node_ip}
                if node_port:
                    entry['address'] = node_address
                active_nodes.append(entry)
            else:
                existing['ip'] = node_ip # Atualiza IP se mudou
                if node_port:
                    existing['address'] = node_address
                
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