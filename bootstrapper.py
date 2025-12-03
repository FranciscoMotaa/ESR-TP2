import socket
import threading
import json
import random
import time
import signal
import sys
import os
import atexit
from datetime import datetime

# Configuração do Tracker
BIND_IP = "0.0.0.0"
BIND_PORT = 6000 # Porta TCP para registo
HEARTBEAT_TIMEOUT = 35  # Segundos sem heartbeat = offline
LOST_TIMEOUT = 60  # Segundos sem heartbeat = perdido

# Lista de nós: [{'id': 'R1', 'ip': '10.0.1.1', 'last_seen': timestamp, 'status': 'alive'}, ...]
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
                # Se houver poucos, devolve todos. Se houver muitos, escolhe 3-4.
                k = min(len(candidates), 4)
                selected = random.sample(candidates, k)
                response_neighbors = [n['ip'] for n in selected]
            
            # 3. Adicionar este novo nó à lista (para os próximos o encontrarem)
            # Evitar duplicados (atualizar se já existe)
            existing = next((item for item in active_nodes if item["id"] == node_id), None)
            if not existing:
                active_nodes.append({
                    'id': node_id, 
                    'ip': node_ip, 
                    'last_seen': time.time(),
                    'status': 'alive'
                })
            else:
                existing['ip'] = node_ip
                existing['last_seen'] = time.time()
                existing['status'] = 'alive'
                
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

def update_node_status():
    """Atualiza o estado de todos os nós baseado no último heartbeat."""
    now = time.time()
    with lock:
        for node in active_nodes:
            time_since_seen = now - node.get('last_seen', 0)
            
            if time_since_seen > LOST_TIMEOUT:
                node['status'] = 'lost'
            elif time_since_seen > HEARTBEAT_TIMEOUT:
                node['status'] = 'offline'
            else:
                node['status'] = 'alive'

def print_status_table():
    """Imprime tabela formatada com estado de todos os nós."""
    with lock:
        nodes_copy = active_nodes.copy()
    
    if not nodes_copy:
        return
    
    print("\n" + "="*70)
    print(f"{'ID':<15} {'IP':<18} {'STATUS':<10} {'LAST SEEN':<20}")
    print("="*70)
    
   
    for node in sorted(nodes_copy, key=lambda x: x['id']):
        node_id = node['id']
        node_ip = node['ip']
        status = node.get('status', 'unknown')
        last_seen = node.get('last_seen', 0)
        
        time_ago = int(time.time() - last_seen)
        if time_ago < 60:
            last_seen_str = f"{time_ago}s ago"
        else:
            last_seen_str = f"{time_ago//60}m {time_ago%60}s ago"
        
        status_display = status.upper()
        
        print(f"{node_id:<15} {node_ip:<18} {status_display:<10} {last_seen_str:<20}")
    
    print("="*70)
    
    # Estatísticas
    alive = sum(1 for n in nodes_copy if n.get('status') == 'alive')
    offline = sum(1 for n in nodes_copy if n.get('status') == 'offline')
    lost = sum(1 for n in nodes_copy if n.get('status') == 'lost')
    
    print(f"Total: {len(nodes_copy)} | Alive: {alive} | Offline: {offline} | Lost: {lost}")
    print("="*70 + "\n")

def monitor_nodes():
    """Thread que monitoriza estado dos nós periodicamente."""
    while True:
        time.sleep(10)  # Atualizar a cada 10 segundos
        update_node_status()
        print_status_table()

def cleanup(signum=None, frame=None):
    """Limpa recursos ao terminar e mata processos filhos."""
    print("\n[*] A terminar Bootstrapper...")
    
    # Tentar matar todos os processos do grupo
    try:
        os.killpg(os.getpgid(os.getpid()), signal.SIGTERM)
    except:
        pass
    
    sys.exit(0)

def setup_process_group():
    """Configura o processo para ter seu próprio grupo."""
    try:
        os.setpgrp()
    except:
        pass

def start_tracker():
    # Configurar process group
    setup_process_group()
    
    # Registar handlers para terminação limpa
    signal.signal(signal.SIGINT, cleanup)   # Ctrl+C
    signal.signal(signal.SIGTERM, cleanup)  # kill
    
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    try:
        server.bind((BIND_IP, BIND_PORT))
    except OSError as e:
        if e.errno == 98:  # Address already in use
            print(f"[ERRO] Porta {BIND_PORT} já está em uso!")
            print("Para libertar a porta:")
            print(f"  sudo lsof -ti :{BIND_PORT} | xargs kill -9")
            print("  ou: pkill -f bootstrapper.py")
            sys.exit(1)
        raise
    
    server.listen(5)
    print(f"[*] Bootstrapper (Tracker) a correr em {BIND_IP}:{BIND_PORT}")
    print(f"[*] Heartbeat timeout: {HEARTBEAT_TIMEOUT}s (offline) | {LOST_TIMEOUT}s (lost)")
    print("[*] Pressiona Ctrl+C para parar\n")
    
    # Iniciar thread de monitorização
    monitor_thread = threading.Thread(target=monitor_nodes, daemon=True)
    monitor_thread.start()
    
    try:
        while True:
            client, addr = server.accept()
            client_handler = threading.Thread(target=handle_client, args=(client, addr))
            client_handler.start()
    except KeyboardInterrupt:
        cleanup()
    finally:
        server.close()

if __name__ == "__main__":
    start_tracker()