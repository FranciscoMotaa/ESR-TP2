# ott_node.py - VERSÃO FINAL (FLOOD PURO + Latência)

import socket
import threading
import time
import json
import sys

# --- CONFIGURAÇÃO ---
BOOTSTRAPPER_ADDR = ('10.0.10.1', 5555) 

class OTTNode:
    
    def __init__(self, node_name, host_ip, host_port):
        self.node_name = node_name
        self.host_ip = host_ip
        self.host_port = int(host_port)
        
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", self.host_port))
        
        print(f"[{self.node_name}] Nó a correr em {self.host_ip}:{self.host_port}")

        # Tabela de Vizinhos: { 'R1': {'addr': (ip, port), 'latencia': 10.0, 'last_seen': time} }
        self.tabela_vizinhos = {}
        self.lock_tabela = threading.Lock()

        # Tabela de Rotas (Preenchida pelo FLOOD)
        # { 'STREAMER1': {'proximo_salto': 'R1', 'custo': 50.0} }
        self.tabela_rotas = {}
        self.lock_rotas = threading.Lock()

        # Gestão de Fluxos
        self.fluxos = {}
        self.lock_fluxos = threading.Lock()
        
        # Base de dados para evitar loops de Flood
        self.lsa_database = {} 
        
        self.running = True

    # --- BOOTSTRAPPER (TCP) ---
    def contactar_bootstrapper(self):
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
                        # CORREÇÃO AQUI: Garantir que 'last_heartbeat' e 'latencia' existem!
                        self.tabela_vizinhos[vizinho['id']] = {
                            'addr': (vizinho['ip'], int(vizinho['port'])),
                            'latencia': 10.0,
                            'last_heartbeat': current_time  # <--- ESTA CHAVE ERA A QUE FALTAVA/ESTAVA ERRADA
                        }
                return True
            else:
                print(f"[{self.node_name}] Erro registo: {resposta.get('message')}")
                return False

        except Exception as e:
            print(f"[{self.node_name}] ERRO fatal ao contactar bootstrapper: {e}")
            return False
        finally:
            sock_tcp.close()

    # --- PROCESSAMENTO DE PACOTES ---
    def processar_pacote(self, data, addr):
        try:
            mensagem = data.decode()
            
            if mensagem.startswith("HEARTBEAT_FROM"):
                self.processar_heartbeat(mensagem.split(" ")[1], addr)

            # --- LÓGICA DE FLOOD (Descobrir o Caminho) ---
            elif mensagem.startswith("FLOOD"):
                # Msg: FLOOD <origem_stream> <custo> <seq>
                # Ex: FLOOD STREAMER1 20 5
                _, origem_stream, custo_str, seq_str = mensagem.split()
                custo_remoto = float(custo_str)
                seq = int(seq_str)
                
                # 1. Verificar se é informação nova (Sequence Number)
                is_new = False
                with self.lock_rotas: # Usamos lock_rotas para proteger a DB de LSAs
                    last_seq = self.lsa_database.get(origem_stream, -1)
                    if seq > last_seq:
                        self.lsa_database[origem_stream] = seq
                        is_new = True
                
                if is_new:
                    # 2. Descobrir quem me enviou isto (será o meu próximo salto)
                    vizinho_remetente = self.obter_nome_vizinho_por_ip(addr)
                    
                    if vizinho_remetente:
                        # Custo = Custo que veio + Latência do link
                        latencia_link = 10.0 
                        with self.lock_tabela:
                            if vizinho_remetente in self.tabela_vizinhos:
                                latencia_link = self.tabela_vizinhos[vizinho_remetente].get('latencia', 10.0)

                        novo_custo = custo_remoto + latencia_link
                        
                        # 3. Atualizar Tabela de Rotas se for melhor caminho
                        melhorou = False
                        with self.lock_rotas:
                            rota_atual = self.tabela_rotas.get(origem_stream)
                            if rota_atual is None or novo_custo < rota_atual['custo']:
                                self.tabela_rotas[origem_stream] = {
                                    'proximo_salto': vizinho_remetente,
                                    'custo': novo_custo
                                }
                                melhorou = True
                                print(f"[{self.node_name}] 🗺️ Rota Flood: Para '{origem_stream}' ir por '{vizinho_remetente}' (Custo {novo_custo:.1f})")
                        
                        # 4. Reencaminhar para vizinhos (se for melhor ou novo)
                        if melhorou:
                            msg_flood = f"FLOOD {origem_stream} {novo_custo} {seq}".encode()
                            self.inundar_vizinhos(msg_flood, ignore_ip=addr)

            elif mensagem.startswith("JOIN"):
                self.tratar_join(mensagem, addr)

            elif mensagem.startswith("STREAM"):
                partes = mensagem.split(" ", 2)
                if len(partes) >= 2:
                    self.reencaminhar_dados(partes[1], data)

        except Exception: pass

    # --- AUXILIARES ---
    def obter_nome_vizinho_por_ip(self, addr):
        with self.lock_tabela:
            for nome, dados in self.tabela_vizinhos.items():
                if dados['addr'] == addr: return nome
        return None

    def processar_heartbeat(self, nome_vizinho, addr):
        now = time.time()
        with self.lock_tabela:
            antigo = next((n for n, d in self.tabela_vizinhos.items() if d['addr'] == addr and n != nome_vizinho), None)
            if antigo:
                print(f"[{self.node_name}] 🔄 Identidade: {antigo} -> {nome_vizinho}")
                self.tabela_vizinhos.pop(antigo)
            
            if nome_vizinho not in self.tabela_vizinhos:
                print(f"[{self.node_name}] Novo vizinho: {nome_vizinho}")

            self.tabela_vizinhos[nome_vizinho] = {'addr': addr, 'latencia': 10.0, 'last_heartbeat': now}

    def inundar_vizinhos(self, mensagem, ignore_ip=None):
        with self.lock_tabela:
            for dados in self.tabela_vizinhos.values():
                if dados['addr'] != ignore_ip:
                    try: self.sock.sendto(mensagem, dados['addr'])
                    except: pass

    # --- LÓGICA DE JOIN (USANDO TABELA DE ROTAS DO FLOOD) ---
    
    def tratar_join(self, msg, addr):
        stream_id = msg.split(" ")[1]
        quem_pediu = self.obter_nome_vizinho_por_ip(addr)
        
        if quem_pediu:
            print(f"[{self.node_name}] 📝 Pedido de {quem_pediu} para {stream_id}")
            try: self.sock.sendto(f"ACK_JOIN {stream_id}".encode(), addr)
            except: pass
            
            with self.lock_fluxos:
                if stream_id not in self.fluxos:
                    self.fluxos[stream_id] = {'downstream': []}
                if quem_pediu not in self.fluxos[stream_id]['downstream']:
                    self.fluxos[stream_id]['downstream'].append(quem_pediu)
            
            if self.node_name == stream_id:
                print(f"[{self.node_name}] 🎬 SOU A FONTE! A iniciar...")
                threading.Thread(target=self.thread_video, args=(stream_id,)).start()
            else:
                # Reencaminhar para cima (Recursivo)
                threading.Thread(target=self.enviar_pedido_join_via_flood, args=(stream_id,)).start()

    def enviar_pedido_join_via_flood(self, stream_id):
        """
        Consulta a tabela de rotas (criada pelo Flood) e envia o JOIN.
        """
        proximo = None
        with self.lock_rotas:
            rota = self.tabela_rotas.get(stream_id)
            if rota:
                proximo = rota['proximo_salto']
        
        if not proximo:
            print(f"[{self.node_name}] ❌ Sem rota Flood para {stream_id}. Aguarde anúncio...")
            return

        # Obter endereço
        addr_prox = None
        with self.lock_tabela:
            if proximo in self.tabela_vizinhos:
                addr_prox = self.tabela_vizinhos[proximo]['addr']
        
        if not addr_prox:
            print(f"[{self.node_name}] ❌ Rota existe ({proximo}), mas vizinho não está vivo.")
            return

        # Envio com Fiabilidade (Stop-and-Wait)
        self.sock.settimeout(2.0)
        for i in range(3):
            try:
                print(f"[{self.node_name}] 📤 JOIN para {proximo} ({i+1}/3)...")
                self.sock.sendto(f"JOIN {stream_id}".encode(), addr_prox)
                d, _ = self.sock.recvfrom(1024)
                if f"ACK_JOIN {stream_id}" in d.decode():
                    print(f"[{self.node_name}] ✅ ACK recebido!")
                    self.sock.settimeout(None)
                    return
            except socket.timeout:
                pass
            except: pass
        
        self.sock.settimeout(None)
        print(f"[{self.node_name}] ❌ Falha ao contactar {proximo}")

    def reencaminhar_dados(self, stream_id, pacote):
        if self.node_name.startswith("C"):
            sys.stdout.write(".")
            sys.stdout.flush()
            return
        
        with self.lock_fluxos:
            if stream_id in self.fluxos:
                for dest in self.fluxos[stream_id]['downstream']:
                    with self.lock_tabela:
                        if dest in self.tabela_vizinhos:
                            try: self.sock.sendto(pacote, self.tabela_vizinhos[dest]['addr'])
                            except: pass

    # --- THREADS ---
    
    def thread_video(self, stream_id):
        seq = 1
        while self.running:
            with self.lock_fluxos:
                if not self.fluxos.get(stream_id, {}).get('downstream'):
                    break # Pára se não houver clientes
            dados = f"Frame_{seq}".ljust(100, '.')
            self.reencaminhar_dados(stream_id, f"STREAM {stream_id} {dados}".encode())
            seq += 1
            time.sleep(0.1)

    def thread_servidor_anunciar(self):
        """SERVIDORES: Enviam FLOOD para anunciar existência."""
        stream_id = self.node_name
        seq = 0
        while self.running:
            time.sleep(10)
            seq += 1
            msg = f"FLOOD {stream_id} 0 {seq}".encode() # Custo inicial 0
            self.inundar_vizinhos(msg)
            print(f"[{self.node_name}] 📢 Anúncio Flood enviado (Seq {seq})")

    def thread_ouvir(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(4096)
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
            mortos = []
            with self.lock_tabela:
                for nome, dados in self.tabela_vizinhos.items():
                    if now - dados['last_heartbeat'] > 20: mortos.append(nome)
            if mortos:
                print(f"[{self.node_name}] 💀 Mortos: {mortos}")
                with self.lock_tabela:
                    for n in mortos: self.tabela_vizinhos.pop(n, None)

    def thread_menu(self):
        time.sleep(2)
        while self.running:
            try:
                print(f"\n[{self.node_name}] 1.Pedir Stream | 2.Ver Rotas Flood | 3.Sair")
                op = input("> ")
                if op == '1':
                    target = input("ID Stream (ex: STREAMER1): ")
                    threading.Thread(target=self.enviar_pedido_join_via_flood, args=(target,)).start()
                elif op == '2':
                    with self.lock_rotas: print(json.dumps(self.tabela_rotas, indent=2))
                elif op == '3':
                    self.stop()
                    break
            except: pass

    def start(self):
        if not self.contactar_bootstrapper(): return
        
        threading.Thread(target=self.thread_ouvir, daemon=True).start()
        threading.Thread(target=self.thread_heartbeat, daemon=True).start()
        threading.Thread(target=self.thread_verificador, daemon=True).start()
        
        if self.node_name.startswith("STREAMER"):
            threading.Thread(target=self.thread_servidor_anunciar, daemon=True).start()
        
        if self.node_name.startswith("C"):
            threading.Thread(target=self.thread_menu, daemon=True).start()
            
        print(f"[{self.node_name}] A correr.")
        try:
            while True: time.sleep(1)
        except: self.stop()

    def stop(self):
        self.running = False
        self.sock.close()

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Uso: python3 ott_node.py <NOME> <IP> <PORTA>")
        sys.exit(1)
    OTTNode(sys.argv[1], sys.argv[2], sys.argv[3]).start()