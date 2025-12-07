import socket
import json
import threading
import sys
import argparse
import time

# --- CONFIGURAÇÃO ---
BOOTSTRAP_PORT = 5555
OVERLAY_PORT = 6000 # Porta que este nó vai usar para aceitar conexões


# Variável global para armazenar os vizinhos ativos
NEIGHBOR_CONNECTIONS = {} # {id: socket_object} 


def register_with_bootstrapper(node_id, node_ip, bootstrapper_ip):
    """Conecta-se ao bootstrapper, regista o nó e obtém a lista de vizinhos."""
    try:
        # 1. Conexão TCP ao Bootstrapper
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        print(f"[{node_id}] Tentando conectar ao Bootstrapper @ {bootstrapper_ip}:{BOOTSTRAP_PORT}...")
        client_socket.connect((bootstrapper_ip, BOOTSTRAP_PORT))
        
        # 2. Enviar mensagem de registo
        reg_message = {
            'id': node_id,
            'ip': node_ip,
            'port': OVERLAY_PORT
        }
        client_socket.sendall(json.dumps(reg_message).encode('utf-8'))
        
        # 3. Receber resposta
        data = client_socket.recv(4096)
        response = json.loads(data.decode('utf-8'))
        client_socket.close()

        if response.get('status') == 'OK':
            print(f"[{node_id}] Registo SUCESSO. {len(response['neighbors'])} vizinhos recebidos.")
            
            # 4. Iniciar conexões com vizinhos
            connect_to_neighbors(node_id, response['neighbors'])
            return True
        else:
            print(f"[{node_id}] ERRO no registo: {response.get('message', 'Resposta inválida')}")
            return False

    except ConnectionRefusedError:
        print(f"[{node_id}] ERRO: Conexão recusada. O Bootstrapper não está ativo em {bootstrapper_ip}.")
        return False
    except Exception as e:
        print(f"[{node_id}] ERRO ao comunicar com Bootstrapper: {e}")
        return False

def connect_to_neighbors(my_id, neighbors_list):
    """Tenta estabelecer conexões de saída com os vizinhos recebidos."""
    
    print(f"[{my_id}] A iniciar conexões de saída (Cliente) com vizinhos:")

    for n in neighbors_list:
        neighbor_id = n['id']
        neighbor_ip = n['ip']
        neighbor_port = n['port']
        
        # Ignorar se já estivermos conectados (se esta função for chamada múltiplas vezes)
        if neighbor_id in NEIGHBOR_CONNECTIONS:
            continue
            
        try:
            # 1. Log da Tentativa
            status = "ATIVO" if neighbor_id != neighbor_ip else "INATIVO (ID baseado no IP)"
            print(f"[{my_id}]   -> Tentando conectar a {neighbor_id} @ {neighbor_ip}:{neighbor_port} ({status})...")

            # 2. Estabelecer Conexão
            neighbor_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Timeout curto para não bloquear
            neighbor_socket.settimeout(2) 
            neighbor_socket.connect((neighbor_ip, neighbor_port))
            
            # 3. Sucesso: Armazenar a conexão
            NEIGHBOR_CONNECTIONS[neighbor_id] = neighbor_socket
            print(f"[{my_id}]   -> SUCESSO: Conexão estabelecida com {neighbor_id}.")
            
            # Opcional: Iniciar um thread para lidar com dados de entrada deste vizinho
            # threading.Thread(target=handle_neighbor_input, args=(neighbor_socket, neighbor_id)).start()

        except socket.timeout:
            print(f"[{my_id}]   -> FALHA: {neighbor_id} @ {neighbor_ip} não respondeu (Timeout).")
        except ConnectionRefusedError:
            # Isto acontece se o vizinho estiver inativo, mas o IP/Porta for resolúvel
            print(f"[{my_id}]   -> FALHA: {neighbor_id} @ {neighbor_ip} recusou a conexão. (Inativo?)")
        except Exception as e:
            print(f"[{my_id}]   -> FALHA: Erro desconhecido com {neighbor_id}: {e}")


def start_overlay_server(my_id, my_ip):
    """Inicia o componente servidor do nó para aceitar conexões de outros vizinhos."""
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Liga ao IP de overlay (p. ex., 10.0.10.1)
        server_socket.bind((my_ip, OVERLAY_PORT)) 
        server_socket.listen()
        print(f"[{my_id}] Servidor Overlay a escutar em {my_ip}:{OVERLAY_PORT}...")
        
        while True:
            conn, addr = server_socket.accept()
            # Quando um vizinho se conecta, iniciamos uma thread para lidar com essa conexão
            # A lógica de identificação do vizinho (via mensagem HELLO, por exemplo) e 
            # de armazenamento em NEIGHBOR_CONNECTIONS teria de ser adicionada aqui.
            print(f"[{my_id}] Conexão de entrada (Servidor) aceita de {addr[0]}:{addr[1]}")
            # threading.Thread(target=handle_incoming_connection, args=(conn, addr)).start()
            
    except Exception as e:
        # Se a porta já estiver em uso, ou outro erro grave.
        print(f"[{my_id}] ERRO fatal no Servidor Overlay: {e}")
    finally:
        server_socket.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Nó da Rede Overlay.')
    # Requer o ID do nó (R3, R7, C1, etc.), o IP de overlay (10.x.x.x) e o IP do bootstrapper
    parser.add_argument('id', type=str, help='ID único do nó (e.g., R3, C1).')
    parser.add_argument('ip', type=str, help='IP de Overlay deste nó (e.g., 10.0.10.1).')
    parser.add_argument('bootstrapper_ip', type=str, help='IP onde o Bootstrapper está a correr.')

    args = parser.parse_args()

    # 1. Inicia o componente servidor em segundo plano (para aceitar vizinhos)
    # ATENÇÃO: É vital que este servidor comece a escutar na sua porta ANTES de tentar conectar-se.
    server_thread = threading.Thread(target=start_overlay_server, args=(args.id, args.ip))
    server_thread.daemon = True # Permite que a thread pare quando o programa principal parar
    server_thread.start()

    # Dá um momento para o socket abrir
    time.sleep(1) 
    
    # 2. Regista-se no bootstrapper (Cliente)
    if register_with_bootstrapper(args.id, args.ip, args.bootstrapper_ip):
        print(f"[{args.id}] Inicialização do nó concluída. Total de conexões de saída estabelecidas: {len(NEIGHBOR_CONNECTIONS)}")
        
    # 3. Mantém o nó ativo (para manter a thread do servidor e as conexões vivas)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n[{args.id}] Encerrando o nó...")
        sys.exit(0) 
    # Dá um momento para o socket abrir
    time.sleep(1) 
    
    # 2. Regista-se no bootstrapper (Cliente)
    if register_with_bootstrapper(args.id, args.ip, args.bootstrapper_ip):
        print(f"[{args.id}] Inicialização do nó concluída. Total de conexões de saída estabelecidas: {len(NEIGHBOR_CONNECTIONS)}")
        
    # 3. Mantém o nó ativo (para manter a thread do servidor e as conexões vivas)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n[{args.id}] Encerrando o nó...")
        sys.exit(0)
