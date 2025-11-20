# ott_node.py

import socket
import threading
import time
import json  # Para falar com o bootstrapper
import sys   # Para ler os argumentos de linha de comando

# Endereço do nosso "porteiro" (o bootstrapper do overlay)
BOOTSTRAPPER_ADDR = ('10.0.10.1', 5555)# ATUALIZADO: Este é o 'overlay_bootstrapper.py'
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
        self.sock.bind(("0.0.0.0", self.host_port))
        
        print(f"[{self.node_name}] Nó a correr em {self.host_ip}:{self.host_port}")

        # --- TAREFA: A TABELA DE VIZINHOS ---
        self.tabela_vizinhos = {}
        self.lock_tabela = threading.Lock()
        
        # --- FIM DA TAREFA ---
        
        self.running = True # Usado para parar as threads

    def contactar_bootstrapper(self):
        """
        Versão TCP para ser compatível com o novo bootstrap_inicio.py
        """
        print(f"[{self.node_name}] A contactar Bootstrapper (TCP) em {BOOTSTRAPPER_ADDR}...")
        
        # Cria um socket TCP temporário apenas para o registo
        sock_tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        
        try:
            sock_tcp.settimeout(5.0)
            sock_tcp.connect(BOOTSTRAPPER_ADDR) # Conecta ao servidor
            
            # Prepara a mensagem JSON como o servidor espera:
            # {'id': 'C2', 'ip': '10.0.19.1', 'port': 5000}
            # (Nota: enviamos a nossa porta UDP para os outros nos contactarem depois)
            mensagem = {
                'id': self.node_name,
                'ip': self.host_ip,
                'port': self.host_port 
            }
            
            sock_tcp.sendall(json.dumps(mensagem).encode('utf-8'))
            
            # Espera pela resposta
            data = sock_tcp.recv(4096)
            resposta = json.loads(data.decode('utf-8'))
            
            if resposta.get('status') == 'OK':
                lista_vizinhos = resposta.get('neighbors', [])
                print(f"[{self.node_name}] Registo OK! Vizinhos recebidos: {len(lista_vizinhos)}")
                
                current_time = time.time()
                
                # Preenche a tabela com a lista recebida
                with self.lock_tabela:
                    for vizinho in lista_vizinhos:
                        # A lista vem como [{'id': 'R1', 'ip': '...', 'port': ...}, ...]
                        nome = vizinho['id']
                        ip = vizinho['ip']
                        porta = vizinho['port']
                        
                        self.tabela_vizinhos[nome] = {
                            'addr': (ip, int(porta)),
                            'last_heartbeat': current_time
                        }
                print(f"[{self.node_name}] Tabela carregada: {list(self.tabela_vizinhos.keys())}")
                return True
            else:
                print(f"[{self.node_name}] Erro no registo: {resposta.get('message')}")
                return False

        except Exception as e:
            print(f"[{self.node_name}] ERRO fatal ao contactar bootstrapper: {e}")
            return False
        finally:
            sock_tcp.close() # Fecha a conexão TCP

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
        TAREFA: Atualiza a tabela de forma INTELIGENTE.
        Se o IP já existir com outro nome, atualiza o nome.
        """
        current_time = time.time()
        
        with self.lock_tabela:
            # 1. Procura se já temos este IP registado com um nome diferente (ex: o IP antigo)
            nome_antigo_para_remover = None
            
            for nome_existente, dados in self.tabela_vizinhos.items():
                # Se o IP e Porta forem iguais... mas o nome for diferente
                if dados['addr'] == addr and nome_existente != nome_vizinho:
                    nome_antigo_para_remover = nome_existente
                    break
            
            # 2. Se encontrámos um nome antigo para este IP, removemo-lo!
            if nome_antigo_para_remover:
                print(f"[{self.node_name}] 🔄 Atualização de Identidade: {nome_antigo_para_remover} mudou para {nome_vizinho}")
                self.tabela_vizinhos.pop(nome_antigo_para_remover)

            # 3. Atualiza (ou cria) a entrada com o nome correto
            if nome_vizinho not in self.tabela_vizinhos:
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

    def thread_verificar_vizinhos(self):
        """
        Trabalhador 3 (Verificador 🕵️‍♂️). 
        Corre periodicamente para remover vizinhos "mortos" a cada 20s.
        """
        print(f"[{self.node_name}] THREAD: Verificador de Vizinhos iniciado.")
        
        # Os teus valores:
        CHECK_INTERVAL = 20   # Acorda e verifica a cada 20s
        TIMEOUT_DURATION = 20 # Se passaram 20s sem heartbeat, considera morto
        
        while self.running:
            
            # 1. Espera o tempo definido antes da próxima verificação
            time.sleep(CHECK_INTERVAL) 
            
            # (Opcional: print de debug para saberes que ele está vivo)
            # print(f"[{self.node_name}] Verificador: A verificar tabela...")
            
            current_time = time.time()
            vizinhos_mortos = [] 
            
            # -------------------------------------------------------
            # FASE 1: DETEÇÃO (Apenas Leitura)
            # -------------------------------------------------------
            with self.lock_tabela:
                # Percorre a tabela para encontrar quem está "velho demais"
                for nome_vizinho, dados_vizinho in self.tabela_vizinhos.items():
                    older = dados_vizinho['last_heartbeat']
                    
                    # A tua comparação lógica:
                    if (current_time - older) >= TIMEOUT_DURATION:
                        vizinhos_mortos.append(nome_vizinho)
            
            # -------------------------------------------------------
            # FASE 2: REMOÇÃO (Apenas Escrita)
            # -------------------------------------------------------
            # Só entramos aqui se encontrámos alguém morto na Fase 1
            if vizinhos_mortos:
                print(f"[{self.node_name}] ALERTA 💀: Vizinhos considerados mortos: {vizinhos_mortos}")
                
                # Precisamos de adquirir o "cadeado" novamente para APAGAR
                with self.lock_tabela:
                    for nome in vizinhos_mortos:
                        # .pop(chave, None) remove a chave se ela existir
                        self.tabela_vizinhos.pop(nome, None)

        
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

        
        # "Contrata" o Trabalhador 3 (Verificador 🕵️‍♂️)
        t_verificar = threading.Thread(target=self.thread_verificar_vizinhos)
        t_verificar.daemon = True
        t_verificar.start()
        # --- FIM DA NOVA LINHA ---

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