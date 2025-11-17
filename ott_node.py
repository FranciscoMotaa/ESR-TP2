# ott_node.py

import socket
import threading
import time
import json  # Para falar com o bootstrapper
import sys   # Para ler os argumentos de linha de comando

# Endereço do nosso "porteiro" (o bootstrapper do overlay)
BOOTSTRAPPER_ADDR = ('10.0.20.10', 9999) # ATUALIZADO: Este é o 'overlay_bootstrapper.py'
                                        # E não o 'bootstrap_inicio.py' (que usa TCP)
                                        # Vamos manter a lógica UDP que tínhamos discutido

class OTTNode:
    
    def __init__(self, node_name, host_ip, host_port):
        self.node_name = node_name
        self.host_ip = host_ip
        self.host_port = host_port
        
        # O socket UDP principal para toda a comunicação
        # (Usamos o mesmo socket para Enviar e Ouvir)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.host_ip, self.host_port))
        
        print(f"[{self.node_name}] Nó a correr em {self.host_ip}:{self.host_port}")

        # --- TAREFA: A TABELA DE VIZINHOS ---
        self.tabela_vizinhos = {}
        self.lock_tabela = threading.Lock()
        
        # --- FIM DA TAREFA ---
        
        self.running = True # Usado para parar as threads

    def contactar_bootstrapper(self):
        """
        PASSO 1: Contacta o 'porteiro' (bootstrapper) para 
        obter a lista de vizinhos e preencher a tabela.
        (Este código está ótimo, mantemos como está)
        """
        print(f"[{self.node_name}] A contactar Bootstrapper em {BOOTSTRAPPER_ADDR}...")
        
        mensagem = f"REGISTER {self.node_name} {self.host_ip} {self.host_port}"
        self.sock.sendto(mensagem.encode(), BOOTSTRAPPER_ADDR)
        
        try:
            self.sock.settimeout(10.0) 
            data, addr = self.sock.recvfrom(1024)
            self.sock.settimeout(None) 
            
            lista_vizinhos = json.loads(data.decode())
            print(f"[{self.node_name}] Bootstrapper respondeu. Vizinhos: {list(lista_vizinhos.keys())}")
            
            current_time = time.time()
            
            with self.lock_tabela:
                for nome, (ip, porta) in lista_vizinhos.items():
                    self.tabela_vizinhos[nome] = {
                        'addr': (ip, porta),
                        'last_heartbeat': current_time 
                    }
            
            print(f"[{self.node_name}] Tabela de Vizinhos inicializada: {self.tabela_vizinhos}")
            return True
            
        except socket.timeout:
            print(f"[{self.node_name}] ERRO: Bootstrapper não respondeu.")
            return False
        except Exception as e:
            print(f"[{self.node_name}] ERRO ao contactar bootstrapper: {e}")
            return False

    # --- INÍCIO DA NOVA LÓGICA DE HEARTBEAT ---

    def processar_pacote(self, data, addr):
        """
        O "Dispatcher"  dispatcher. 
        Esta função é chamada pelo Ouvinte (Thread 2)
        """
        try:
            mensagem = data.decode()
            
            # É uma mensagem de Heartbeat?
            if mensagem.startswith("HEARTBEAT_FROM"):
                partes = mensagem.split(" ")
                if len(partes) == 2:
                    nome_vizinho = partes[1]
                    self.processar_heartbeat_recebido(nome_vizinho, addr)
            
            # elif mensagem.startswith("JOIN"):
            #     # Futuramente, aqui processamos os pedidos de stream
            #     pass
                
        except UnicodeDecodeError:
            pass # Ignora pacotes que não são texto (ex: futuro stream)
        except Exception as e:
            print(f"[{self.node_name}] Erro a processar pacote: {e}")

    def processar_heartbeat_recebido(self, nome_vizinho, addr):
        """
        TAREFA: Atualiza a tabela quando recebe um heartbeat.
        """
        current_time = time.time()
        
        # Usa o "cadeado" 🔐 para mexer na tabela em segurança
        with self.lock_tabela:
            if nome_vizinho in self.tabela_vizinhos:
                # Se já o conhecemos, apenas atualizamos o timestamp
                self.tabela_vizinhos[nome_vizinho]['last_heartbeat'] = current_time
            else:
                # Se é um vizinho novo, adicionamo-lo
                print(f"[{self.node_name}] Novo vizinho detetado via Heartbeat: {nome_vizinho}")
                self.tabela_vizinhos[nome_vizinho] = {
                    'addr': addr,
                    'last_heartbeat': current_time
                }

    def thread_ouvir_pacotes(self):
        """
        Trabalhador 2 (Ouvinte 🎧). 
        Fica sempre à escuta de pacotes UDP.
        """
        print(f"[{self.node_name}] THREAD: Ouvinte iniciado.")
        while self.running:
            try:
                # Esta linha "bloqueia" ⏸️ até um pacote chegar
                data, addr = self.sock.recvfrom(2048) 
                
                # Assim que chega, envia-o para o "dispatcher"
                if self.running:
                    self.processar_pacote(data, addr)
                    
            except socket.timeout:
                continue # Ignora timeouts, volta a ouvir
            except Exception as e:
                if self.running:
                    print(f"[{self.node_name}] Erro no Ouvinte: {e}")

    def thread_enviar_heartbeats(self):
        """
        Trabalhador 1 (Emissor ❤️). 
        Envia heartbeats a todos os vizinhos a cada 5s.
        """
        print(f"[{self.node_name}] THREAD: Emissor de Heartbeats iniciado.")
        
        while self.running:
            time.sleep(5) # Espera 5 segundos
            
            mensagem = f"HEARTBEAT_FROM {self.node_name}".encode()
            
            # Usa o "cadeado" 🔐 para LER a tabela
            with self.lock_tabela:
                # Faz uma cópia da lista de endereços para enviar
                lista_de_enderecos = [dados['addr'] for dados in self.tabela_vizinhos.values()]

            # Envia para todos (fora do "lock" para não bloquear a tabela)
            for addr in lista_de_enderecos:
                try:
                    self.sock.sendto(mensagem, addr)
                except Exception as e:
                    print(f"[{self.node_name}] Erro ao enviar HB para {addr}: {e}")

    def thread_verficar_vizinhos(self):
        
    # --- FIM DA NOVA LÓGICA DE HEARTBEAT ---

    def start(self):
        """
        Arranca o nó e (AGORA) inicia as threads de trabalho.
        """
        
        if not self.contactar_bootstrapper():
            print(f"[{self.node_name}] A encerrar. Não foi possível contactar o bootstrapper.")
            return

        print(f"[{self.node_name}] A iniciar threads de trabalho...")

        # "Contrata" o Trabalhador 1 (Emissor)
        t_enviar = threading.Thread(target=self.thread_enviar_heartbeats)
        t_enviar.daemon = True # Morre com o programa principal
        t_enviar.start()

        # "Contrata" o Trabalhador 2 (Ouvinte)
        t_ouvir = threading.Thread(target=self.thread_ouvir_pacotes)
        t_ouvir.daemon = True
        t_ouvir.start()

        print(f"[{self.node_name}] Nó operacional. A correr.")
        
        # A thread principal pode agora sair, ou fazer outras coisas
        # (Vamos mantê-la viva para apanhar o Ctrl+C)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        print(f"[{self.node_name}] A encerrar...")
        self.running = False
        self.sock.close()


# --- Bloco Principal de Execução (Sem alterações) ---
if __name__ == "__main__":
    
    if len(sys.argv) != 4:
        print("Uso: python3 ott_node.py <meu_nome> <meu_ip> <minha_porta>")
        sys.exit(1)
        
    MEU_NOME = sys.argv[1]
    MEU_IP = sys.argv[2]
    MINHA_PORTA = int(sys.argv[3])

    no = OTTNode(node_name=MEU_NOME, host_ip=MEU_IP, host_port=MINHA_PORTA)
    no.start()