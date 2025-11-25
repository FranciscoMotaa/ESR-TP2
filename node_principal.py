import socket
import threading
import time
import json
import sys
import math
import estado_global
import plano_controlo

# --- CONFIGURAÇÃO ---
# Endereço do Bootstrapper (assumido: 10.0.10.1:5555)
BOOTSTRAPPER_ADDR = ('10.0.10.1', 5555) 
NODE_STATE = None # Será inicializado em __init__

class OTTNode:
    
    def __init__(self, node_name, host_ip, host_port):
        
        global NODE_STATE
        # Inicializa o estado global
        NODE_STATE = estado_global.OverlayNode(node_name, int(host_port))
        
        self.node_name = node_name
        self.host_ip = host_ip
        self.host_port = int(host_port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", self.host_port))
        
        print(f"[{self.node_name}] Nó a correr em {self.host_ip}:{self.host_port}")
        
    def _get_neighbor_by_addr(self, addr):
        """Função auxiliar para encontrar o nome do vizinho pelo endereço IP/Porta."""
        with NODE_STATE.lock_vizinhos:
            for nome, dados in NODE_STATE.tabela_vizinhos.items():
                # Compara o endereço (IP, Porta)
                if dados['addr'] == addr:
                    return nome
        return None
        
    # Função auxiliar para enviar UDP a partir dos módulos (usada pelo plano_controlo.py)
    def _send_udp_internal(self, ip, port, message_bytes):
        """Função interna segura para enviar pacotes UDP."""
        try:
            self.sock.sendto(message_bytes, (ip, port))
        except Exception as e:
            # Em ambientes simulados (como VMs que se desligam) isto é normal
            print(f"[{self.node_name}] Erro de socket ao enviar para {ip}:{port}: {e}")

    # --- REGISTO INICIAL (TCP) ---
    def contactar_bootstrapper(self):
        """Contacta o Bootstrapper para registo e obtenção de vizinhos."""
        try:
            client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            print(f"[{self.node_name}] A contactar o Bootstrapper @ {BOOTSTRAPPER_ADDR}...")
            client_socket.connect(BOOTSTRAPPER_ADDR)
            
            reg_message = {
                'id': self.node_name,
                'ip': self.host_ip,
                'port': self.host_port
            }
            client_socket.sendall(json.dumps(reg_message).encode('utf-8'))
            
            data = client_socket.recv(4096)
            response = json.loads(data.decode('utf-8'))
            client_socket.close()

            if response.get('status') == 'OK':
                print(f"[{self.node_name}] Registo SUCESSO. {len(response['neighbors'])} vizinhos recebidos.")
                
                # INICIALIZAÇÃO CORRIGIDA da Tabela de Vizinhos (tabela_vizinhos)
                with NODE_STATE.lock_vizinhos:
                    for n in response['neighbors']:
                        viz_id = n['id']
                        addr = (n['ip'], int(n['port']))
                        
                        # Inicializa o estado do vizinho para medição de métrica e LSA
                        NODE_STATE.tabela_vizinhos[viz_id] = {
                            'addr': addr,
                            'custo_link': float('inf'), # Começa em infinito, atualiza no primeiro PONG
                            'latencia': 0.0,
                            'pings_enviados': 0,
                            'pongs_recebidos': 0,
                            'last_seen': time.time()
                        }
                        print(f"[{self.node_name}] Adicionado vizinho {viz_id} @ {n['ip']}:{n['port']}")
                
                return True
            else:
                print(f"[{self.node_name}] ERRO no registo: {response.get('message', 'Resposta inválida')}")
                return False

        except Exception as e:
            print(f"[{self.node_name}] ERRO ao comunicar com Bootstrapper: {e}")
            return False
            
    # --- THREADS COMUNS ---

    def thread_ouvir(self):
        """Loop principal para escutar pacotes UDP e delegar o processamento."""
        print(f"[{self.node_name}] A escutar pacotes UDP em {self.host_ip}:{self.host_port}...")
        
        # Define um timeout para permitir que o socket seja fechado e o ciclo interrompido.
        self.sock.settimeout(1.0) 
        
        while NODE_STATE.running:
            try:
                data, addr = self.sock.recvfrom(4096)
                # Processa o pacote numa nova thread para não bloquear a escuta
                threading.Thread(target=self.processar_pacote, args=(data, addr), daemon=True).start()
                
            except socket.timeout:
                # O timeout de 1 segundo permite verificar NODE_STATE.running
                pass 
            except Exception as e:
                if NODE_STATE.running:
                    print(f"[{self.node_name}] Erro ao ouvir ou processar pacote: {e}")
                break 


    def thread_medir_latencia(self):
        """Envia PINGs e mede Latência (T) e Perda (P) para calcular o Custo de Link (C)."""
        seq = 0
        while NODE_STATE.running:
            time.sleep(2) 
            seq += 1
            ts = time.time()
            msg = f"PING {self.node_name} {seq} {ts}".encode()
            
            with NODE_STATE.lock_vizinhos:
                # Cria uma lista de vizinhos para evitar modificar o dicionário enquanto itera
                vizinhos_a_pingar = list(NODE_STATE.tabela_vizinhos.items())

                for viz_name, d in vizinhos_a_pingar:
                    # 1. Atualizar contagem
                    d['pings_enviados'] = d.get('pings_enviados', 0) + 1
                    
                    # 2. Calcular e atualizar métrica (A cada 5 PINGs)
                    if d['pings_enviados'] >= 5: 
                        
                        pings = d['pings_enviados']
                        pongs = d.get('pongs_recebidos', 0)
                        perda = 1 - (pongs / pings)
                        latencia = d['latencia'] # Latência já é média móvel
                        
                        # CUSTO COMBINADO (C = T / (1 - P))
                        # Se a perda for muito alta (e.g., > 99%), define o custo como infinito.
                        if perda >= 0.99: 
                            custo = float('inf')
                        else:
                            custo = latencia / (1 - perda)
                            
                        d['perda'] = perda
                        d['custo_link'] = custo
                        print(f"[{self.node_name}] 📶 Vizinho {viz_name}: Custo={custo:.1f}ms (Lat={latencia:.1f}ms, P={perda:.0%})")

                        # Reiniciar contadores para o próximo ciclo de amostragem
                        d['pings_enviados'] = 0
                        d['pongs_recebidos'] = 0

                    try: 
                        self.sock.sendto(msg, d['addr'])
                    except: 
                        # Ignora erro se o vizinho sair ou o socket falhar
                        pass


    # --- PROCESSAMENTO DE PACOTES E ROTEAMENTO ---

    def processar_pacote(self, data, addr):
        try:
            mensagem = data.decode('utf-8')
            
            # Tenta deserializar como LSA (JSON) primeiro
            try:
                msg_dict = json.loads(mensagem)
                msg_type = msg_dict.get('type')
                
                if msg_type == 'LSA':
                    # Lógica de roteamento Link State (OSPF/A*)
                    plano_controlo.process_lsa(msg_dict, addr, self._send_udp_internal)
                    return
            except json.JSONDecodeError:
                # Não é um LSA
                pass
                
            # 1. PING (Resposta)
            if mensagem.startswith("PING"):
                _, origem, seq, ts_envio = mensagem.split()
                # A resposta PONG contém o timestamp original
                resp = f"PONG {self.node_name} {ts_envio}".encode('utf-8')
                self.sock.sendto(resp, addr)

            # 2. PONG (Recálculo da Latência e Perda)
            elif mensagem.startswith("PONG"):
                _, origem, ts_envio_str = mensagem.split()
                rtt = (time.time() - float(ts_envio_str)) * 1000 # RTT em milissegundos
                latencia_calculada = rtt / 2
                
                with NODE_STATE.lock_vizinhos:
                    if origem in NODE_STATE.tabela_vizinhos:
                        d = NODE_STATE.tabela_vizinhos[origem]
                        d['addr'] = addr # Confirma o endereço
                        d['last_seen'] = time.time()
                        d['pongs_recebidos'] = d.get('pongs_recebidos', 0) + 1
                        
                        # Média móvel exponencial para latência (alpha=0.3)
                        antiga = d['latencia']
                        d['latencia'] = (antiga * 0.7) + (latencia_calculada * 0.3)
                            
            # 3. JOIN (Adesão à Stream)
            elif mensagem.startswith("JOIN"):
                partes = mensagem.split(" ")
                if len(partes) < 2: return
                stream_id = partes[1]
                
                vizinho_nome = self._get_neighbor_by_addr(addr)
                
                if vizinho_nome:
                    print(f"[{self.node_name}]  Pedido JOIN de {vizinho_nome} para {stream_id}")
                    
                    # Adiciona o vizinho à lista downstream (quem recebe o stream de mim)
                    with NODE_STATE.lock_fluxos:
                        if stream_id not in NODE_STATE.fluxos:
                            NODE_STATE.fluxos[stream_id] = {'downstream': []}
                        if vizinho_nome not in NODE_STATE.fluxos[stream_id]['downstream']:
                            NODE_STATE.fluxos[stream_id]['downstream'].append(vizinho_nome)

                    # Se o nó for a FONTE da stream, não faz nada (apenas espera o pedido). 
                    # Se não for a FONTE, envia o JOIN para o próximo salto upstream.
                    if self.node_name != stream_id:
                        # Este nó precisa da stream. Procura upstream.
                        self.enviar_pedido_join_upstream(stream_id)
                        
            # 4. STREAM (Dados de Vídeo)
            elif mensagem.startswith("STREAM"):
                partes = mensagem.split(" ", 2)
                if len(partes) >= 2:
                    stream_id = partes[1]
                    self.reencaminhar_dados(stream_id, data) # 'data' é o pacote bruto (bytes)
                    
        except Exception as e:
            # Erros de parsing ou outros problemas
            print(f"[{self.node_name}] Erro ao processar pacote de {addr}: {e}")


    def enviar_pedido_join_upstream(self, stream_id):
        """
        Calcula o caminho de menor custo (Latência/Perda) usando A* e envia JOIN 
        ao próximo salto (upstream) em direção à fonte.
        """
        
        # 1. Executa o algoritmo A* (Dijkstra)
        # Nota: Calculamos a rota da Fonte (stream_id) até a nós (self.node_name)
        proximo_salto, custo_total = plano_controlo.calculate_a_star_route(stream_id, self.node_name)
        
        
        if proximo_salto and proximo_salto != 'SELF':
            
            with NODE_STATE.lock_vizinhos:
                target_info = NODE_STATE.tabela_vizinhos.get(proximo_salto)
            
            if target_info:
                addr = target_info['addr']
                
                # Cache da rota
                with NODE_STATE.lock_rotas_cache:
                    NODE_STATE.tabela_rotas_cache[stream_id] = {
                        'proximo_salto': proximo_salto,
                        'custo_total': custo_total
                    }
                    
                print(f"[{self.node_name}] ⬆️ Rota A*: Upstream={proximo_salto} (Custo: {custo_total:.1f}ms). Enviando JOIN...")
                
                # Envia o pedido JOIN ao próximo salto upstream
                self._send_udp_internal(addr[0], addr[1], f"JOIN {stream_id}".encode('utf-8'))
                
            else:
                print(f"[{self.node_name}] Erro: Próximo salto {proximo_salto} não encontrado na tabela de vizinhos.")
        else:
            print(f"[{self.node_name}] Erro: Não foi encontrada rota para {stream_id}. Aguardando LSA...")

    def reencaminhar_dados(self, stream_id, pacote_bruto):
        """Reencaminha o pacote de stream para todos os nós downstream registados."""
        
        # 1. Obtém os vizinhos downstream para esta stream
        with NODE_STATE.lock_fluxos:
            if stream_id not in NODE_STATE.fluxos:
                return
            downstream_nodes = NODE_STATE.fluxos[stream_id]['downstream']
            
        # 2. Envia para cada vizinho downstream
        with NODE_STATE.lock_vizinhos:
            for vizinho_nome in downstream_nodes:
                if vizinho_nome in NODE_STATE.tabela_vizinhos:
                    addr = NODE_STATE.tabela_vizinhos[vizinho_nome]['addr']
                    # Reencaminha o pacote (bytes brutos)
                    self._send_udp_internal(addr[0], addr[1], pacote_bruto)
                # else: Nó downstream saiu ou não existe mais, deve ser tratado com um LEAVE (não implementado aqui)


    # --- THREADS ESPECÍFICAS (Streamer/Cliente) ---
    
    def thread_gerar_video(self):
        """Simula a geração de dados de vídeo e envia-os downstream."""
        stream_id = self.node_name # A stream tem o nome do streamer
        seq = 0
        while NODE_STATE.running:
            time.sleep(0.5) # Envia a cada 500ms
            seq += 1
            
            # Formato do pacote: STREAM <StreamerID> <Data>
            pacote = f"STREAM {stream_id} {self.node_name} Frame #{seq}".encode('utf-8')
            
            # Envia o pacote para os nós downstream que fizeram JOIN
            self.reencaminhar_dados(stream_id, pacote)
            if seq % 20 == 0:
                print(f"[{self.node_name}] 🎬 Streaming: Frame #{seq}")


    def thread_menu(self):
        """Permite ao utilizador interagir (pedir uma stream)."""
        time.sleep(5) # Espera que o registo e a primeira LSA ocorram
        
        while NODE_STATE.running:
            try:
                # O input() é bloqueante, mas é suficiente para este exercício
                stream_id = input(f"\n[{self.node_name}] Qual stream quer ver? (Ex: STREAMER1): ").upper().strip()
                if stream_id and stream_id != self.node_name:
                    self.enviar_pedido_join_upstream(stream_id)
                elif stream_id == self.node_name:
                    print("Não pode pedir stream a si próprio.")
                elif not stream_id:
                    pass
                else:
                    print("ID de stream inválido.")
            except EOFError:
                # Trata Ctrl+D
                break
            except Exception as e:
                print(f"Erro no menu: {e}")
                break
            
    # --- LAUNCH & SHUTDOWN ---

    def start(self):
        if not self.contactar_bootstrapper(): return

        # Threads comuns a todos
        threading.Thread(target=self.thread_ouvir, daemon=True).start()
        threading.Thread(target=self.thread_medir_latencia, daemon=True).start()
        
        # Thread de Anúncio LSA (para todos os nós)
        threading.Thread(target=plano_controlo.thread_lsa_anuncio, 
                         args=(self._send_udp_internal,), 
                         daemon=True).start()

        # Threads Específicas
        if self.node_name.startswith("STREAMER"):
            threading.Thread(target=self.thread_gerar_video, daemon=True).start()
        
        # O menu interativo só é lançado para Clientes (C) ou se não for streamer
        if self.node_name.startswith("C") or not self.node_name.startswith("STREAMER"):
            threading.Thread(target=self.thread_menu, daemon=True).start()

        print(f"[{self.node_name}] Operacional. Pressione Ctrl+C para sair.")
        try:
            while NODE_STATE.running: time.sleep(1)
        except KeyboardInterrupt: 
            self.stop()
        except:
             self.stop()


    def stop(self):
        NODE_STATE.running = False
        try:
            self.sock.close()
        except:
            pass
        print(f"\n[{self.node_name}] Encerrado.")

# --- EXECUÇÃO ---

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Uso: python3 ott_node.py <NOME> <IP> <PORTA>")
        sys.exit(1)
    
    # É uma boa prática envolver a inicialização num try/except
    try:
        node = OTTNode(sys.argv[1], sys.argv[2], sys.argv[3])
        node.start()
    except Exception as e:
        print(f"Erro fatal: {e}")
        sys.exit(1)
