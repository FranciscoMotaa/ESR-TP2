# ott_node.py

import socket
import threading
import time
import json  # Para falar com o bootstrapper
import sys   # Para ler os argumentos de linha de comando

# Endereço do nosso "porteiro" (o bootstrapper do overlay)
# Muda este IP para o IP da máquina onde o 'overlay_bootstrapper.py' corre
BOOTSTRAPPER_ADDR = ('10.0.20.10', 9999) 

class OTTNode:
    
    def __init__(self, node_name, host_ip, host_port):
        self.node_name = node_name
        self.host_ip = host_ip
        self.host_port = host_port
        
        # O socket UDP principal para toda a comunicação
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.host_ip, self.host_port))
        
        print(f"[{self.node_name}] Nó a correr em {self.host_ip}:{self.host_port}")

        # --- TAREFA: A TABELA DE VIZINHOS ---
        
        # 1. A Estrutura de Dados (o Dicionário)
        # Começa vazia. Vai ser preenchida pelo bootstrapper.
        self.tabela_vizinhos = {}
        
        # 2. O "Cadeado" (O Lock)
        # Essencial para proteger a tabela das threads
        self.lock_tabela = threading.Lock()
        
        # --- FIM DA TAREFA ---

    def contactar_bootstrapper(self):
        """
        PASSO 1: Contacta o 'porteiro' (bootstrapper) para 
        obter a lista de vizinhos e preencher a tabela.
        """
        print(f"[{self.node_name}] A contactar Bootstrapper em {BOOTSTRAPPER_ADDR}...")
        
        # 1. Envia mensagem de registo
        # Formato: "REGISTER <nome> <meu_ip> <minha_porta>"
        mensagem = f"REGISTER {self.node_name} {self.host_ip} {self.host_port}"
        self.sock.sendto(mensagem.encode(), BOOTSTRAPPER_ADDR)
        
        try:
            # 2. Espera pela resposta (com um timeout de 10s)
            self.sock.settimeout(10.0) 
            data, addr = self.sock.recvfrom(1024)
            self.sock.settimeout(None) # Remove o timeout
            
            # A resposta esperada é um JSON com os vizinhos
            # Ex: {'R1': ['10.0.1.10', 5000], 'R3': ['10.0.1.12', 5000]}
            lista_vizinhos = json.loads(data.decode())
            
            print(f"[{self.node_name}] Bootstrapper respondeu. Vizinhos: {list(lista_vizinhos.keys())}")
            
            # --- TAREFA: PREENCHER A TABELA ---
            
            # 3. Pega na resposta e preenche a tabela de vizinhos
            current_time = time.time()
            
            # Usa o "cadeado" para escrever na tabela em segurança
            with self.lock_tabela:
                for nome, (ip, porta) in lista_vizinhos.items():
                    self.tabela_vizinhos[nome] = {
                        'addr': (ip, porta),
                        'last_heartbeat': current_time # Assume que estão vivos agora
                    }
            
            # --- FIM DA TAREFA ---
            
            print(f"[{self.node_name}] Tabela de Vizinhos inicializada: {self.tabela_vizinhos}")
            return True
            
        except socket.timeout:
            print(f"[{self.node_name}] ERRO: Bootstrapper não respondeu.")
            return False
        except Exception as e:
            print(f"[{self.node_name}] ERRO ao contactar bootstrapper: {e}")
            return False

    def start(self):
        """Arranca o nó."""
        
        # A primeira coisa que fazemos é preencher a nossa tabela de vizinhos
        if not self.contactar_bootstrapper():
            print(f"[{self.node_name}] A encerrar. Não foi possível contactar o bootstrapper.")
            return

        # ... (Aqui é onde vamos iniciar as threads de Heartbeat, A*, etc.) ...
        print(f"[{self.node_name}] Nó operacional. A correr.")
        
        # Por agora, fica só num loop (depois será a thread de "Ouvir")
        try:
            while True:
                # A thread principal ficará aqui à escuta de pacotes
                # data, addr = self.sock.recvfrom(2048) 
                # self.processar_pacote(data, addr)
                time.sleep(1) 
        except KeyboardInterrupt:
            print(f"[{self.node_name}] A encerrar.")

# --- Bloco Principal de Execução ---
if __name__ == "__main__":
    
    # Ex: python3 ott_node.py C2 10.0.1.14 5000
    if len(sys.argv) != 4:
        print("Uso: python3 ott_node.py <meu_nome> <meu_ip> <minha_porta>")
        sys.exit(1)
        
    MEU_NOME = sys.argv[1]
    MEU_IP = sys.argv[2]
    MINHA_PORTA = int(sys.argv[3])

    # 1. Cria a instância do nó
    no = OTTNode(node_name=MEU_NOME, host_ip=MEU_IP, host_port=MINHA_PORTA)
    
    # 2. Arranca o nó (ele vai contactar o bootstrapper)
    no.start()