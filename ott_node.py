# ott_node.py - Versão Final Completa

import socket
import threading
import time
import json
import sys
import heapq  # Necessário para o algoritmo A*

# --- CONFIGURAÇÃO ---
# Endereço do Bootstrapper (R3)
BOOTSTRAPPER_ADDR = ('10.0.10.1', 5555) 
CONFIG_FILE = 'bootstrap_conf.json'

class OTTNode:
    
    def __init__(self, node_name, host_ip, host_port):
        self.node_name = node_name
        self.host_ip = host_ip
        self.host_port = int(host_port)
        
        # O socket UDP principal para toda a comunicação
        # '0.0.0.0' permite receber pacotes em qualquer interface
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", self.host_port))
        
        print(f"[{self.node_name}] Nó a correr em {self.host_ip}:{self.host_port}")

        # --- ESTRUTURAS DE DADOS ---
        self.tabela_vizinhos = {} # { 'R1': {'addr': (ip, port), 'last_heartbeat': 0} }
        self.lock_tabela = threading.Lock()

        self.fluxos = {} # { 'STREAMER1': {'downstream': [], 'active': False} }
        self.lock_fluxos = threading.Lock()
        
        # Carregar o Grafo da Rede para o A*
        self.grafo_rede = self.carregar_grafo()
        
        self.running = True

    # --- A* ALGORITMO DE ROTEAMENTO ---
    
    def carregar_grafo(self):
        """
        Lê o JSON e constrói o grafo de adjacência { 'R1': ['R3', 'C6'] }.
        Isto permite ao nó saber a topologia completa para calcular rotas.
        """
        grafo = {}
        try:
            with open(CONFIG_FILE, 'r') as f:
                dados = json.load(f)
            # Converte a lista de nós num dicionário ID -> Vizinhos
            for node in dados.get("nodes", []):
                grafo[node['id']] = node['neighbors']
            print(f"[{self.node_name}] 🗺️ Grafo de rede carregado ({len(grafo)} nós).")
            return grafo
        except Exception as e:
            print(f"[{self.node_name}] ❌ Erro ao carregar grafo: {e}")
            return {}

    def calcular_caminho_a_star(self, destino):
        """
        Executa A* para encontrar o caminho mais curto (menos saltos) até ao destino.
        Retorna uma lista: ['Origem', 'Salto1', 'Salto2', 'Destino']
        """
        inicio = self.node_name
        
        # Verificações de segurança
        if inicio not in self.grafo_rede:
            print(f"[{self.node_name}] ⚠️ A*: Eu ({inicio}) não estou no grafo JSON.")
            return None
        if destino not in self.grafo_rede:
            print(f"[{self.node_name}] ⚠️ A*: Destino ({destino}) não existe no grafo JSON.")
            return None

        # Fila de prioridade: (custo_total, nó_atual)
        fila = [(0, inicio)]
        pais = {inicio: None}
        custo_acumulado = {inicio: 0}

        while fila:
            _, atual = heapq.heappop(fila)

            if atual == destino:
                # Reconstruir o caminho de trás para a frente
                caminho = []
                while atual:
                    caminho.append(atual)
                    atual = pais[atual]
                return caminho[::-1] # Inverter para ficar [Origem -> Destino]

            # Explorar vizinhos
            for vizinho in self.grafo_rede.get(atual, []):
                novo_custo = custo_acumulado[atual] + 1 # Custo 1 por salto
                
                if vizinho not in custo_acumulado or novo_custo < custo_acumulado[vizinho]:
                    custo_acumulado[vizinho] = novo_custo
                    prioridade = novo_custo
                    heapq.heappush(fila, (prioridade, vizinho))
                    pais[vizinho] = atual
        
        return None # Caminho não encontrado

    # --- BOOTSTRAPPER & REGISTO ---

    def contactar_bootstrapper(self):
        """Liga-se ao R3 (TCP) para anunciar presença e receber vizinhos."""
        print(f"[{self.node_name}] A contactar Bootstrapper (TCP) em {BOOTSTRAPPER_ADDR}...")
        sock_tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock_tcp.settimeout(5.0)
            sock_tcp.connect(BOOTSTRAPPER_ADDR)
            
            mensagem = {'id': self.node_name, 'ip': self.host_ip, 'port': self.host_port}
            sock_tcp.sendall(json.dumps(mensagem).encode('utf-8'))
            
            data = sock_tcp.recv(4096)
            resposta = json.loads(data.decode('utf-8'))
            
            if resposta.get('status') == 'OK':
                lista_vizinhos = resposta.get('neighbors', [])
                print(f"[{self.node_name}] Registo OK! Vizinhos recebidos: {len(lista_vizinhos)}")
                current_time = time.time()
                with self.lock_tabela:
                    for vizinho in lista_vizinhos:
                        self.tabela_vizinhos[vizinho['id']] = {
                            'addr': (vizinho['ip'], int(vizinho['port'])),
                            'last_heartbeat': current_time
                        }
                return True
            return False
        except Exception as e:
            print(f"[{self.node_name}] ERRO ao contactar bootstrapper: {e}")
            return False
        finally:
            sock_tcp.close()

    # --- LÓGICA DE PACOTES (UDP) ---

    def processar_pacote(self, data, addr):
        """Dispatcher principal para pacotes UDP recebidos."""
        try:
            mensagem = data.decode()
            
            # --- HEARTBEATS ---
            if mensagem.startswith("HEARTBEAT_FROM"):
                self.processar_heartbeat_recebido(mensagem.split(" ")[1], addr)
            
            # --- PEDIDOS JOIN ---
            elif mensagem.startswith("JOIN"):
                partes = mensagem.split(" ")
                stream_id = partes[1]
                
                # 1. Descobrir quem enviou (pelo IP)
                vizinho_nome = None
                with self.lock_tabela:
                    for nome, dados in self.tabela_vizinhos.items():
                        if dados['addr'] == addr:
                            vizinho_nome = nome
                            break
                
                if vizinho_nome:
                    print(f"[{self.node_name}] 📝 Pedido de {vizinho_nome} para {stream_id}")
                    
                    # 2. Enviar ACK imediato
                    try:
                        self.sock.sendto(f"ACK_JOIN {stream_id}".encode(), addr)
                    except: pass
                    
                    # 3. Registar na tabela de fluxos
                    with self.lock_fluxos:
                        if stream_id not in self.fluxos:
                            self.fluxos[stream_id] = {'downstream': [], 'active': False}
                        if vizinho_nome not in self.fluxos[stream_id]['downstream']:
                            self.fluxos[stream_id]['downstream'].append(vizinho_nome)
                    
                    # 4. Decisão de Roteamento
                    if self.node_name == stream_id:
                        # Sou a FONTE
                        print(f"[{self.node_name}] 🎬 SOU A FONTE! A iniciar stream...")
                        threading.Thread(target=self.thread_enviar_stream_fake, args=(stream_id,)).start()
                    else:
                        # Sou um ROUTER -> Reencaminhar para cima
                        self.enviar_pedido_join(stream_id)

            # --- DADOS DE STREAM ---
            elif mensagem.startswith("STREAM"):
                partes = mensagem.split(" ", 2)
                if len(partes) >= 2:
                    stream_id = partes[1]
                    self.reencaminhar_dados(stream_id, data)

        except UnicodeDecodeError:
            pass # Ignora pacotes corrompidos
        except Exception as e:
            print(f"[{self.node_name}] Erro processar: {e}")

    def processar_heartbeat_recebido(self, nome_vizinho, addr):
        current_time = time.time()
        with self.lock_tabela:
            # Correção de Identidade: Se o IP já existe com outro nome, remove o antigo
            antigo = next((n for n, d in self.tabela_vizinhos.items() if d['addr'] == addr and n != nome_vizinho), None)
            if antigo:
                print(f"[{self.node_name}] 🔄 Atualização: {antigo} mudou para {nome_vizinho}")
                self.tabela_vizinhos.pop(antigo)
            
            if nome_vizinho not in self.tabela_vizinhos:
                 print(f"[{self.node_name}] Novo vizinho detetado: {nome_vizinho}")

            self.tabela_vizinhos[nome_vizinho] = {'addr': addr, 'last_heartbeat': current_time}

    def reencaminhar_dados(self, stream_id, pacote_bruto):
        """Reencaminha pacotes para todos na lista 'downstream'."""
        if self.node_name.startswith("C"):
            print(f"[{self.node_name}] 📺 A REPRODUZIR: {len(pacote_bruto)} bytes")
            return

        with self.lock_fluxos:
            if stream_id in self.fluxos:
                for dest in self.fluxos[stream_id]['downstream']:
                    with self.lock_tabela:
                        if dest in self.tabela_vizinhos:
                            try:
                                self.sock.sendto(pacote_bruto, self.tabela_vizinhos[dest]['addr'])
                            except: pass

    def thread_enviar_stream_fake(self, stream_id):
        """Gera vídeo falso se este nó for o Streamer."""
        seq = 1
        while self.running:
            with self.lock_fluxos:
                # Se não tiver clientes, pára de enviar para poupar recursos
                if not self.fluxos.get(stream_id, {}).get('downstream'):
                    print("⏸️ Sem clientes. Pausa no stream.")
                    break
            
            dados = f"Frame_{seq}".ljust(100, '.')
            mensagem = f"STREAM {stream_id} {dados}".encode()
            self.reencaminhar_dados(stream_id, mensagem)
            seq += 1
            time.sleep(0.1)

    # --- ROTEAMENTO DINÂMICO (A AÇÃO REAL) ---

    def enviar_pedido_join(self, stream_id):
        """Usa A* para encontrar o caminho e envia JOIN com Stop-and-Wait."""
        
        # 1. CALCULAR ROTA REAL
        caminho = self.calcular_caminho_a_star(stream_id)
        
        if not caminho or len(caminho) < 2:
            print(f"[{self.node_name}] ❌ Erro: Não há rota para {stream_id} (Caminho: {caminho})")
            return

        proximo_salto = caminho[1] # O vizinho imediato no caminho
        print(f"[{self.node_name}] 🛤️ Rota A* calculada: {caminho} -> Próximo: {proximo_salto}")

        # 2. Obter endereço do vizinho
        endereco_vizinho = None
        with self.lock_tabela:
            if proximo_salto in self.tabela_vizinhos:
                endereco_vizinho = self.tabela_vizinhos[proximo_salto]['addr']
            else:
                print(f"[{self.node_name}] ❌ Vizinho '{proximo_salto}' ainda não está vivo na tabela!")
                return

        # 3. Enviar com Fiabilidade (Stop-and-Wait)
        self.sock.settimeout(2.0)
        MAX_TENTATIVAS = 3
        
        for i in range(MAX_TENTATIVAS):
            try:
                print(f"[{self.node_name}] 📤 JOIN para {proximo_salto} (Tentativa {i+1}/{MAX_TENTATIVAS})...")
                self.sock.sendto(f"JOIN {stream_id}".encode(), endereco_vizinho)
                
                data, _ = self.sock.recvfrom(1024)
                resposta = data.decode()
                
                if f"ACK_JOIN {stream_id}" in resposta:
                    print(f"[{self.node_name}] ✅ ACK recebido de {proximo_salto}!")
                    self.sock.settimeout(None)
                    return # Sucesso
                    
            except socket.timeout:
                print(f"[{self.node_name}] ⚠️ Timeout à espera de ACK.")
            except Exception as e:
                print(f"[{self.node_name}] Erro envio: {e}")
        
        self.sock.settimeout(None)
        print(f"[{self.node_name}] ❌ Falha crítica: {proximo_salto} não respondeu.")

    # --- THREADS DE SUPORTE ---
    
    def thread_ouvir(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(2048)
                self.processar_pacote(data, addr)
            except: pass

    def thread_heartbeat(self):
        while self.running:
            time.sleep(5)
            with self.lock_tabela:
                addrs = [d['addr'] for d in self.tabela_vizinhos.values()]
            for addr in addrs:
                try: self.sock.sendto(f"HEARTBEAT_FROM {self.node_name}".encode(), addr)
                except: pass

    def thread_verificador(self):
        while self.running:
            time.sleep(10)
            now = time.time()
            dead = []
            with self.lock_tabela:
                for nome, dados in self.tabela_vizinhos.items():
                    if now - dados['last_heartbeat'] > 20: dead.append(nome)
            if dead:
                print(f"[{self.node_name}] 💀 Mortos: {dead}")
                with self.lock_tabela:
                    for n in dead: self.tabela_vizinhos.pop(n, None)

    def thread_menu(self):
        time.sleep(2)
        while self.running:
            try:
                op = input(f"\n({self.node_name}) 1.Pedir Stream | 2.Sair > ")
                if op == '1':
                    target = input("Qual Stream? (ex: STREAMER1): ")
                    # Inicia o pedido numa thread para não bloquear o menu
                    threading.Thread(target=self.enviar_pedido_join, args=(target,)).start()
                elif op == '2':
                    self.stop()
                    break
            except: pass

    def start(self):
        if not self.contactar_bootstrapper(): return
        
        threading.Thread(target=self.thread_ouvir, daemon=True).start()
        threading.Thread(target=self.thread_heartbeat, daemon=True).start()
        threading.Thread(target=self.thread_verificador, daemon=True).start()
        
        if self.node_name.startswith("C"):
            threading.Thread(target=self.thread_menu, daemon=True).start()
            
        print(f"[{self.node_name}] A correr.")
        try:
            while True: time.sleep(1)
        except: self.stop()

    def stop(self):
        self.running = False
        self.sock.close()
        print("Encerrado.")

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Uso: python3 ott_node.py <NOME> <IP> <PORTA>")
        sys.exit(1)
    OTTNode(sys.argv[1], sys.argv[2], sys.argv[3]).start()