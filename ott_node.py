# ott_node.py - VERSÃO FINAL COM FLOOD E LATÊNCIA

import socket
import threading
import time
import json
import sys

# --- CONFIGURAÇÃO ---
# Endereço do Bootstrapper (R3) - Tem de bater certo com o IP onde corres o bootstrapper.py
BOOTSTRAPPER_ADDR = ('10.0.10.1', 5555) 

class OTTNode:
    
    def __init__(self, node_name, host_ip, host_port):
        self.node_name = node_name
        self.host_ip = host_ip
        self.host_port = int(host_port)
        
        # O socket UDP principal para toda a comunicação
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", self.host_port))
        
        print(f"[{self.node_name}] Nó a correr em {self.host_ip}:{self.host_port}")

        # --- ESTRUTURAS DE DADOS ---
        # Tabela de Vizinhos: { 'R1': {'addr': (ip, port), 'latencia': 10.0, 'last_seen': time} }
        self.tabela_vizinhos = {}
        self.lock_tabela = threading.Lock()

        # Tabela de Rotas (Preenchida pelo FLOOD): { 'STREAM1': {'proximo_salto': 'R1', 'custo': 50.0} }
        self.tabela_rotas = {}
        self.lock_rotas = threading.Lock()

        # Gestão de Fluxos (Quem pediu o quê): { 'STREAM1': {'downstream': ['C1', 'R2']} }
        self.fluxos = {}
        self.lock_fluxos = threading.Lock()
        
        self.running = True

    # --- REGISTO INICIAL (TCP) ---
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
                        self.tabela_vizinhos[vizinho['id']] = {
                            'addr': (vizinho['ip'], int(vizinho['port'])),
                            'latencia': 10.0, # Valor default conservador (10ms)
                            'last_seen': current_time
                        }
                return True
            else:
                print(f"[{self.node_name}] Erro registo: {resposta.get('message')}")
                return False
        except Exception as e:
            print(f"[{self.node_name}] ERRO fatal Bootstrapper: {e}")
            return False
        finally:
            sock_tcp.close()

    # --- PROCESSAMENTO DE PACOTES (UDP) ---
    def processar_pacote(self, data, addr):
        try:
            mensagem = data.decode()
            
            # 1. PING (Medição de Latência)
            if mensagem.startswith("PING"):
                # Msg: PING <origem> <seq> <timestamp_envio>
                _, origem, seq, ts_envio = mensagem.split()
                # Responde imediatamente com PONG, devolvendo o timestamp original
                resp = f"PONG {self.node_name} {ts_envio}".encode()
                self.sock.sendto(resp, addr)

            # 2. PONG (Resposta da Medição)
            elif mensagem.startswith("PONG"):
                # Msg: PONG <origem> <timestamp_original>
                _, origem, ts_envio_str = mensagem.split()
                # Calcula RTT e Latência (RTT / 2)
                rtt = (time.time() - float(ts_envio_str)) * 1000 # Em ms
                latencia = rtt / 2
                
                with self.lock_tabela:
                    if origem in self.tabela_vizinhos:
                        # Atualiza endereço se mudou
                        self.tabela_vizinhos[origem]['addr'] = addr
                        self.tabela_vizinhos[origem]['last_seen'] = time.time()
                        # Média móvel para suavizar a latência
                        antiga = self.tabela_vizinhos[origem]['latencia']
                        self.tabela_vizinhos[origem]['latencia'] = (antiga * 0.7) + (latencia * 0.3)

            # 3. FLOOD (Descoberta de Rotas)
            elif mensagem.startswith("FLOOD"):
                # Msg: FLOOD <stream_id> <custo_acumulado> <sender_id>
                _, stream_id, custo_remoto_str, sender_id = mensagem.split()
                custo_remoto = float(custo_remoto_str)

                # Descobrir latência do link de onde veio
                latencia_link = 999.0
                with self.lock_tabela:
                    if sender_id in self.tabela_vizinhos:
                        latencia_link = self.tabela_vizinhos[sender_id]['latencia']
                        self.tabela_vizinhos[sender_id]['last_seen'] = time.time() # Refresh vizinho

                novo_custo_total = custo_remoto + latencia_link
                
                melhorou = False
                with self.lock_rotas:
                    rota_atual = self.tabela_rotas.get(stream_id)
                    # Se rota não existe OU nova rota é mais rápida (menor latência)
                    if rota_atual is None or novo_custo_total < rota_atual['custo']:
                        self.tabela_rotas[stream_id] = {
                            'proximo_salto': sender_id,
                            'custo': novo_custo_total,
                            'addr_vizinho': addr # Guarda IP para enviar JOIN depois
                        }
                        melhorou = True
                        print(f"[{self.node_name}] 🗺️ Rota '{stream_id}' via {sender_id} (Lat: {novo_custo_total:.1f}ms)")

                # Se melhorou, inunda os vizinhos (Split Horizon)
                if melhorou:
                    msg_flood = f"FLOOD {stream_id} {novo_custo_total} {self.node_name}".encode()
                    with self.lock_tabela:
                        for viz_nome, viz_dados in self.tabela_vizinhos.items():
                            if viz_nome != sender_id:
                                try: self.sock.sendto(msg_flood, viz_dados['addr'])
                                except: pass

            # 4. JOIN (Adesão à Stream)
            elif mensagem.startswith("JOIN"):
                # Msg: JOIN <stream_id>
                partes = mensagem.split(" ")
                stream_id = partes[1]
                
                # Descobrir quem enviou
                vizinho_nome = None
                with self.lock_tabela:
                    for nome, dados in self.tabela_vizinhos.items():
                        if dados['addr'] == addr:
                            vizinho_nome = nome
                            break
                
                if vizinho_nome:
                    print(f"[{self.node_name}] 📝 Pedido JOIN de {vizinho_nome} para {stream_id}")
                    
                    # Adiciona à lista downstream
                    with self.lock_fluxos:
                        if stream_id not in self.fluxos:
                            self.fluxos[stream_id] = {'downstream': []}
                        if vizinho_nome not in self.fluxos[stream_id]['downstream']:
                            self.fluxos[stream_id]['downstream'].append(vizinho_nome)

                    # Se sou a FONTE, começo a enviar. Se não, peço para cima.
                    if self.node_name == stream_id:
                        print(f"[{self.node_name}] 🎬 SOU A FONTE! Cliente ligado.")
                    else:
                        self.enviar_pedido_join_upstream(stream_id)

            # 5. STREAM (Dados de Vídeo)
            elif mensagem.startswith("STREAM"):
                partes = mensagem.split(" ", 2)
                if len(partes) >= 2:
                    stream_id = partes[1]
                    self.reencaminhar_dados(stream_id, data)

        except Exception as e:
            # print(f"Erro processar: {e}") # Debug ruidoso
            pass

    # --- LÓGICA DE ENVIO ---
    
    def enviar_pedido_join_upstream(self, stream_id):
        """Olha para a tabela de rotas e envia JOIN para o melhor vizinho."""
        target_info = None
        with self.lock_rotas:
            if stream_id in self.tabela_rotas:
                target_info = self.tabela_rotas[stream_id]
        
        if target_info:
            proximo_nome = target_info['proximo_salto']
            
            addr_lista = target_info.get('addr_vizinho')
            
            if addr_lista:
                addr = tuple(addr_lista) # Converter lista [ip, port] para tuplo (ip, port)
                print(f"[{self.node_name}] ⬆️ Enviando JOIN {stream_id} para {proximo_nome} ({addr})")
                
                try:
                    self.sock.sendto(f"JOIN {stream_id}".encode(), addr)
                except Exception as e:
                    print(f"[{self.node_name}] ❌ Erro socket: {e}")
            else:
                # Fallback antigo (só se não houver endereço na rota)
                print(f"[{self.node_name}] ⚠️ Rota sem endereço. A tentar tabela de vizinhos...")
                with self.lock_tabela:
                    if proximo_nome in self.tabela_vizinhos:
                        addr = self.tabela_vizinhos[proximo_nome]['addr']
                        self.sock.sendto(f"JOIN {stream_id}".encode(), addr)
                    else:
                        print(f"[{self.node_name}] ❌ Erro: Não consigo contactar {proximo_nome}.")

        else:
            print(f"[{self.node_name}] ❌ Erro: Não tenho rota para {stream_id}. Aguardando FLOOD...")

    def reencaminhar_dados(self, stream_id, pacote_bruto):
        """Se for cliente, reproduz. Se for router, reencaminha."""
        if self.node_name.startswith("C"):
            # Simula reprodução
            # print(f"[{self.node_name}] 📺 Frame recebido stream {stream_id}")
            sys.stdout.write(".") # Efeito visual de loading
            sys.stdout.flush()
            return

        with self.lock_fluxos:
            if stream_id in self.fluxos:
                for dest in self.fluxos[stream_id]['downstream']:
                    with self.lock_tabela:
                        if dest in self.tabela_vizinhos:
                            try: self.sock.sendto(pacote_bruto, self.tabela_vizinhos[dest]['addr'])
                            except: pass

    # --- THREADS ---

    def thread_medir_latencia(self):
        """Envia PINGs periodicamente para atualizar custos."""
        seq = 0
        while self.running:
            time.sleep(2) # A cada 2 segundos
            seq += 1
            ts = time.time()
            msg = f"PING {self.node_name} {seq} {ts}".encode()
            
            with self.lock_tabela:
                # Copia para lista para não bloquear
                addrs = [d['addr'] for d in self.tabela_vizinhos.values()]
            
            for addr in addrs:
                try: self.sock.sendto(msg, addr)
                except: pass

    def thread_servidor_anunciar(self):
        """APENAS PARA O SERVIDOR: Inicia o FLOOD."""
        stream_id = self.node_name # Assumindo que o nome do servidor é o ID da stream (ex: STREAMER1)
        print(f"[{self.node_name}] 📢 Thread de Anúncio FLOOD iniciada.")
        while self.running:
            # Envia FLOOD com custo 0 para todos os vizinhos
            msg = f"FLOOD {stream_id} 0 {self.node_name}".encode()
            with self.lock_tabela:
                for d in self.tabela_vizinhos.values():
                    try: self.sock.sendto(msg, d['addr'])
                    except: pass
            time.sleep(5) # Anuncia a cada 5s

    def thread_ouvir(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(4096)
                self.processar_pacote(data, addr)
            except: pass

    def thread_gerar_video(self):
        """APENAS PARA O SERVIDOR: Gera frames."""
        seq = 1
        stream_id = self.node_name
        while self.running:
            tem_clientes = False
            with self.lock_fluxos:
                if stream_id in self.fluxos and self.fluxos[stream_id]['downstream']:
                    tem_clientes = True
            
            if tem_clientes:
                payload = f"Frame_{seq}".ljust(100, '.')
                msg = f"STREAM {stream_id} {payload}".encode()
                self.reencaminhar_dados(stream_id, msg)
                seq += 1
            time.sleep(0.1) # 10 FPS

    def thread_menu(self):
        time.sleep(1)
        while self.running:
            print(f"\n[{self.node_name}] 1.Pedir Stream | 2.Ver Rotas | 3.Sair")
            op = input("> ")
            if op == '1':
                target = input("ID Stream (ex: STREAMER1): ")
                self.enviar_pedido_join_upstream(target)
            elif op == '2':
                with self.lock_rotas:
                    print(json.dumps(self.tabela_rotas, indent=2))
            elif op == '3':
                self.stop()
                break

    def start(self):
        if not self.contactar_bootstrapper(): return

        # Threads comuns a todos
        threading.Thread(target=self.thread_ouvir, daemon=True).start()
        threading.Thread(target=self.thread_medir_latencia, daemon=True).start()

        # Threads Específicas
        if self.node_name.startswith("STREAMER"):
            threading.Thread(target=self.thread_servidor_anunciar, daemon=True).start()
            threading.Thread(target=self.thread_gerar_video, daemon=True).start()
        
        if self.node_name.startswith("C"):
            threading.Thread(target=self.thread_menu, daemon=True).start()

        print(f"[{self.node_name}] Operacional.")
        try:
            while self.running: time.sleep(1)
        except: self.stop()

    def stop(self):
        self.running = False
        self.sock.close()
        print("Bye.")

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Uso: python3 ott_node.py <NOME> <IP> <PORTA>")
        sys.exit(1)
    OTTNode(sys.argv[1], sys.argv[2], sys.argv[3]).start()