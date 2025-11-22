# ott_node.py

import socket
import threading
import time
import json
import sys
import heapq

# --- CONFIGURAÇÃO ---
# Endereço do Bootstrapper (R3)
BOOTSTRAPPER_ADDR = ('10.0.10.1', 5555) 

class OTTNode:
    
    def __init__(self, node_name, host_ip, host_port):
        self.node_name = node_name
        self.host_ip = host_ip
        self.host_port = host_port
        
        # O socket UDP principal para toda a comunicação
        # '0.0.0.0' permite receber pacotes em qualquer interface
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", self.host_port))
        
        print(f"[{self.node_name}] Nó a correr em {self.host_ip}:{self.host_port}")

        # --- ESTRUTURAS DE DADOS ---
        self.tabela_vizinhos = {}
        self.lock_tabela = threading.Lock()

        self.fluxos = {}
        self.lock_fluxos = threading.Lock()
        
        self.running = True

    def contactar_bootstrapper(self):
        """
        Regista-se no Bootstrapper via TCP e obtém a lista inicial de vizinhos.
        """
        print(f"[{self.node_name}] A contactar Bootstrapper (TCP) em {BOOTSTRAPPER_ADDR}...")
        
        sock_tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        
        try:
            sock_tcp.settimeout(5.0)
            sock_tcp.connect(BOOTSTRAPPER_ADDR)
            
            mensagem = {
                'id': self.node_name,
                'ip': self.host_ip,
                'port': self.host_port 
            }
            
            sock_tcp.sendall(json.dumps(mensagem).encode('utf-8'))
            
            data = sock_tcp.recv(4096)
            resposta = json.loads(data.decode('utf-8'))
            
            if resposta.get('status') == 'OK':
                lista_vizinhos = resposta.get('neighbors', [])
                print(f"[{self.node_name}] Registo OK! Vizinhos recebidos: {len(lista_vizinhos)}")
                
                current_time = time.time()
                
                with self.lock_tabela:
                    for vizinho in lista_vizinhos:
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
            sock_tcp.close()

    # --- LÓGICA DE REDE (UDP) ---

    def processar_pacote(self, data, addr):
        """
        Dispatcher principal para pacotes UDP recebidos.
        """
        try:
            mensagem = data.decode()
            
            if mensagem.startswith("HEARTBEAT_FROM"):
                partes = mensagem.split(" ")
                if len(partes) == 2:
                    nome_vizinho = partes[1]
                    self.processar_heartbeat_recebido(nome_vizinho, addr)
            
            elif mensagem.startswith("JOIN"):
                # 1. Ler os dados
                partes = mensagem.split(" ")
                stream_id = partes[1]
                
                # 2. Registar QUEM pediu (Downstream)
                with self.lock_tabela:
                    # Procura quem tem este IP
                    vizinho_nome = None
                    for nome, dados in self.tabela_vizinhos.items():
                        if dados['addr'] == addr:
                            vizinho_nome = nome
                            break
                
                if vizinho_nome:
                    print(f"[{self.node_name}] 📝 Registando pedido de {vizinho_nome} para stream {stream_id}")

                    with self.lock_fluxos:
                        if stream_id not in self.fluxos:
                            self.fluxos[stream_id] = {'upstream': None, 'downstream': [], 'active': False}
                        
                        if vizinho_nome not in self.fluxos[stream_id]['downstream']:
                            self.fluxos[stream_id]['downstream'].append(vizinho_nome)

                        if self.node_name.startswith("STREAMER"):
                            print(f"[{self.node_name}] 🎬 CLIENTE DETETADO! A iniciar transmissão do {stream_id}...")
                            threading.Thread(target=self.thread_enviar_stream_fake, args=(stream_id,)).start()

                    # 3. REENCAMINHAR (A Estafeta 🏃‍♂️)
                    # Se eu não sou o Streamer, tenho de pedir a alguém!
                    if not self.node_name.startswith("STREAMER"):
                        print(f"[{self.node_name}] 🔄 Reencaminhando pedido para cima...")
                        # Chamamos a mesma função de envio!
                        # Como a lista simulada tem ['C6', 'R1', 'R3'...]
                        # O R1 vai ver que o próximo é o R3 e envia para lá automatically.
                        self.enviar_pedido_join(stream_id)
                else:
                    print(f"[{self.node_name}] Recebi JOIN de desconhecido: {addr}")


            elif mensagem.startswith("STREAM"):
                        # --- NOVA LÓGICA DE DADOS ---
                        # Formato: "STREAM S1 <dados...>"
                        # Cuidado: O payload pode ser binário (vídeo), o decode() pode falhar se for vídeo real.
                        # Por agora, vamos assumir texto/simulação.
                        partes = mensagem.split(" ", 2) # Divide só nos primeiros 2 espaços
                        if len(partes) >= 2:
                            stream_id = partes[1]
                            conteudo = partes[2] if len(partes) > 2 else ""
                            
                            self.reencaminhar_dados(stream_id, data)
                
        except UnicodeDecodeError:
            pass 
        except Exception as e:
            print(f"[{self.node_name}] Erro a processar pacote: {e}")

    def construir_grafo_da_rede(self):
        """
        Lê o JSON e constrói um grafo onde chaves e valores são IDs (Nomes).
        Resolve o problema de traduzir IPs para Nomes.
        """
        grafo = {}
        ip_para_nome = {}

        try:
            # 1. Ler o ficheiro
            with open('bootstrap_conf.json', 'r') as f:
                dados = json.load(f)
            
            lista_nos = dados.get("nodes", [])

            # 2. Primeira Passagem: Criar um mapa de tradução IP -> Nome
            #    (Precisamos disto porque a lista de vizinhos usa IPs)
            for no in lista_nos:
                # Vamos assumir que o IP principal do nó é o primeiro IP da lista de vizinhos
                # ou, idealmente, o JSON deveria ter um campo "ip".
                # COMO O TEU JSON NÃO TEM O PRÓPRIO IP EXPLICITO, 
                # vamos ter de usar uma lógica de detetive ou alterar o JSON.
                
                # --- SOLUÇÃO DE CONTORNO ---
                # Vamos assumir que conseguimos deduzir quem é quem.
                # Mas o ideal era o teu JSON ter: "id": "R1", "ip": "10.0.1.1"
                pass 
                
            # ⚠️ PAUSA: O teu JSON atual torna isto difícil.
            # Ele diz: "R3 tem vizinhos [10.0.10.2, 10.0.8.1]"
            # Mas não diz explicitamente qual é o IP do R3.
            
        except Exception as e:
            print(f"Erro grafo: {e}")
            return {}
    
    def processar_heartbeat_recebido(self, nome_vizinho, addr):
        """
        Atualiza a tabela e corrige nomes se necessário (IP -> Nome).
        """
        current_time = time.time()
        
        with self.lock_tabela:
            # 1. Verificar se este IP já existe com outro nome (ex: IP antigo do bootstrapper)
            nome_antigo_para_remover = None
            
            for nome_existente, dados in self.tabela_vizinhos.items():
                if dados['addr'] == addr and nome_existente != nome_vizinho:
                    nome_antigo_para_remover = nome_existente
                    break
            
            if nome_antigo_para_remover:
                print(f"[{self.node_name}] 🔄 Atualização de Identidade: {nome_antigo_para_remover} mudou para {nome_vizinho}")
                self.tabela_vizinhos.pop(nome_antigo_para_remover)

            if nome_vizinho not in self.tabela_vizinhos:
                 print(f"[{self.node_name}] Novo vizinho detetado via Heartbeat: {nome_vizinho}")

            self.tabela_vizinhos[nome_vizinho] = {
                'addr': addr,
                'last_heartbeat': current_time
            }


    def reencaminhar_dados(self, stream_id, pacote_bruto):
        """
        Recebe dados de stream e reencaminha para todos os interessados (downstream).
        """
        # Se eu sou o destino final (Cliente)
        if self.node_name.startswith("C"):
            print(f"[{self.node_name}] 📺 A REPRODUZIR: Recebi dados do {stream_id} ({len(pacote_bruto)} bytes)")
            return

        # Se eu sou Router/Server, reencaminho
        with self.lock_fluxos:
            if stream_id in self.fluxos:
                destinos = self.fluxos[stream_id]['downstream']
                if destinos:
                    #print(f"[{self.node_name}] ⏩ Reencaminhando dados para {destinos}")
                    with self.lock_tabela:
                        for destino_nome in destinos:
                            if destino_nome in self.tabela_vizinhos:
                                addr = self.tabela_vizinhos[destino_nome]['addr']
                                self.sock.sendto(pacote_bruto, addr)


    # --- THREADS (TRABALHADORES) ---

    def thread_ouvir_pacotes(self):
        """Trabalhador 2 (Ouvinte)."""
        print(f"[{self.node_name}] THREAD: Ouvinte iniciado.")
        while self.running:
            try:
                data, addr = self.sock.recvfrom(2048) 
                if self.running:
                    self.processar_pacote(data, addr)
            except socket.timeout:
                continue 
            except Exception as e:
                if self.running:
                    print(f"[{self.node_name}] Erro no Ouvinte: {e}")

    def thread_enviar_heartbeats(self):
        """Trabalhador 1 (Emissor). Envia a cada 5s."""
        print(f"[{self.node_name}] THREAD: Emissor de Heartbeats iniciado.")
        while self.running:
            time.sleep(5)
            mensagem = f"HEARTBEAT_FROM {self.node_name}".encode()
            
            with self.lock_tabela:
                lista_de_enderecos = [dados['addr'] for dados in self.tabela_vizinhos.values()]

            for addr in lista_de_enderecos:
                try:
                    self.sock.sendto(mensagem, addr)
                except Exception as e:
                    print(f"[{self.node_name}] Erro ao enviar HB para {addr}: {e}")

    def thread_verificar_vizinhos(self):
        """Trabalhador 3 (Verificador). Remove mortos a cada 20s."""
        print(f"[{self.node_name}] THREAD: Verificador de Vizinhos iniciado.")
        CHECK_INTERVAL = 20   
        TIMEOUT_DURATION = 20 
        
        while self.running:
            time.sleep(CHECK_INTERVAL) 
            current_time = time.time()
            vizinhos_mortos = [] 
            
            with self.lock_tabela:
                for nome_vizinho, dados_vizinho in self.tabela_vizinhos.items():
                    older = dados_vizinho['last_heartbeat']
                    if (current_time - older) >= TIMEOUT_DURATION:
                        vizinhos_mortos.append(nome_vizinho)
            
            if vizinhos_mortos:
                print(f"[{self.node_name}] ALERTA 💀: Vizinhos considerados mortos: {vizinhos_mortos}")
                with self.lock_tabela:
                    for nome in vizinhos_mortos:
                        self.tabela_vizinhos.pop(nome, None)

    def thread_menu_cliente(self):
        """Trabalhador 4 (Interface). Apenas para Clientes."""
        print(f"[{self.node_name}] THREAD: Menu iniciado.")
        time.sleep(2) 

        while self.running:
            print("\nOptions:")
            print("1 - List available streams")
            print("2 - Start a stream")
            print("3 - Stop current stream")
            print("4 - Exit")
            
            try:
                choice = input(f"({self.node_name}) Choose an option: ")
                
                if choice == '1':
                    print("TODO: Listar streams conhecidos")
                elif choice == '2':
                    stream_id = input("Qual o ID da stream? (ex: S1): ")
                    self.enviar_pedido_join(stream_id)
                elif choice == '3':
                    print("TODO: Parar stream")
                elif choice == '4':
                    print("A sair...")
                    self.stop()
                    break
            except ValueError:
                pass
            except Exception as e:
                print(f"Erro no menu: {e}")

    def thread_enviar_stream_fake(self, stream_id):
        """
        Simula o envio de um vídeo (envia pacotes numerados).
        """
        seq = 1
        print(f"[{self.node_name}] 🚀 INICIANDO STREAM {stream_id}")
        
        while self.running:
            # Verifica se ainda tenho alguém a quem enviar
            tem_clientes = False
            with self.lock_fluxos:
                if stream_id in self.fluxos and self.fluxos[stream_id]['downstream']:
                    tem_clientes = True
            
            if not tem_clientes:
                print(f"[{self.node_name}] ⏸️ Sem clientes. A pausar stream.")
                break

            # Cria o pacote de dados
            dados = f"Frame_{seq}".ljust(100, '.') # Cria uma string com 100 chars
            mensagem = f"STREAM {stream_id} {dados}".encode()
            
            # Envia para os vizinhos diretos interessados (ex: R7)
            self.reencaminhar_dados(stream_id, mensagem)
            
            seq += 1
            time.sleep(0.1) # 10 Frames por segundo (Simulação)


    # --- SIMULAÇÃO DE ROTEAMENTO ---

    def enviar_pedido_join(self, stream_id):
        """
        SIMULAÇÃO: Envia JOIN para o próximo salto usando caminho fixo.
        Caminho: C6 -> R1 -> R3 -> R7 -> S1
        """
        caminho_simulado = ['C6', 'R1', 'R3', 'R7', 'STREAMER1']
        
        print(f"[{self.node_name}] A simular rota: {caminho_simulado}")

        if self.node_name not in caminho_simulado:
            print("Erro: Eu não estou neste caminho simulado!")
            return
            
        meu_index = caminho_simulado.index(self.node_name)
        
        if meu_index == len(caminho_simulado) - 1:
            print("Eu sou o destino final (Streamer).")
            return

        proximo_salto = caminho_simulado[meu_index + 1]
        
        endereco_vizinho = None
        with self.lock_tabela:
            if proximo_salto in self.tabela_vizinhos:
                endereco_vizinho = self.tabela_vizinhos[proximo_salto]['addr']
            else:
                print(f"[{self.node_name}] ❌ ERRO: Vizinho '{proximo_salto}' não encontrado! (R1 está ligado?)")
                print(f"Vizinhos conhecidos: {list(self.tabela_vizinhos.keys())}")
                return

        try:
            msg = f"JOIN {stream_id}".encode()
            self.sock.sendto(msg, endereco_vizinho)
            print(f"[{self.node_name}] 📤 JOIN enviado para {proximo_salto} ({endereco_vizinho})")
        except Exception as e:
            print(f"[{self.node_name}] Erro no envio: {e}")

    # --- CONTROLO PRINCIPAL ---

    def start(self):
        if not self.contactar_bootstrapper():
            print(f"[{self.node_name}] A encerrar. Não foi possível contactar o bootstrapper.")
            return

        print(f"[{self.node_name}] A iniciar threads de trabalho...")

        # Iniciar Trabalhadores
        t_enviar = threading.Thread(target=self.thread_enviar_heartbeats)
        t_enviar.daemon = True 
        t_enviar.start()

        t_ouvir = threading.Thread(target=self.thread_ouvir_pacotes)
        t_ouvir.daemon = True
        t_ouvir.start()

        t_verificar = threading.Thread(target=self.thread_verificar_vizinhos)
        t_verificar.daemon = True
        t_verificar.start()

        # Só mostra o menu se o nome começar por "C"
        if self.node_name.startswith("C"): 
            t_menu = threading.Thread(target=self.thread_menu_cliente)
            t_menu.daemon = True
            t_menu.start()

        print(f"[{self.node_name}] Nó operacional. A correr.")
        
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        print(f"[{self.node_name}] A encerrar...")
        self.running = False
        self.sock.close()

# --- EXECUÇÃO ---
if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Uso: python3 ott_node.py <meu_nome> <meu_ip> <minha_porta>")
        sys.exit(1)
        
    MEU_NOME = sys.argv[1]
    MEU_IP = sys.argv[2]
    MINHA_PORTA = int(sys.argv[3])

    no = OTTNode(node_name=MEU_NOME, host_ip=MEU_IP, host_port=MINHA_PORTA)
    no.start()