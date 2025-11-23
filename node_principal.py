# ott_node.py (Parte 1: Inicialização e Métrica)
import socket
import threading
import time
import json
import sys
import math
import estado_global # Importa o novo módulo de estado
import plano_contolo

# --- CONFIGURAÇÃO ---
BOOTSTRAPPER_ADDR = ('10.0.10.1', 5555) 

class OTTNode:
    
    def __init__(self, node_name, host_ip, host_port):
        
        # Inicializa a variável de estado global
        global NODE_STATE
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
                if dados['addr'] == addr:
                    return nome
        return None
        
    # --- REGISTO INICIAL (TCP - Não alterado) ---
    def contactar_bootstrapper(self):
        # ... (O seu código contactar_bootstrapper permanece o mesmo)
        # O código deve popular NODE_STATE.tabela_vizinhos
        pass 
        
    # --- THREADS (Medição de Latência e Perda) ---

    def thread_medir_latencia(self):
        """Envia PINGs e mede Latência (T) e Perda (P) para calcular o Custo de Link (C)."""
        seq = 0
        while NODE_STATE.running:
            time.sleep(2) 
            seq += 1
            ts = time.time()
            msg = f"PING {self.node_name} {seq} {ts}".encode()
            
            with NODE_STATE.lock_vizinhos:
                for viz_name, d in NODE_STATE.tabela_vizinhos.items():
                    # 1. Atualizar contagem
                    d['pings_enviados'] = d.get('pings_enviados', 0) + 1
                    
                    # 2. Calcular e atualizar métrica
                    if d['pings_enviados'] >= 5: # Começar a calcular após 5 pings
                        perda = 1 - (d.get('pongs_recebidos', 0) / d['pings_enviados'])
                        latencia = d['latencia']
                        
                        # CUSTO COMBINADO (C = T * 1/(1-P))
                        if perda >= 0.99: # 99% de perda: custo infinito
                            custo = float('inf')
                        else:
                            custo = latencia * (1 / (1 - perda))
                            
                        d['perda'] = perda
                        d['custo_link'] = custo
                        
                        # Reiniciar contadores após atualização (para média móvel temporal)
                        if seq % 10 == 0:
                             d['pings_enviados'] = 0
                             d['pongs_recebidos'] = 0

                    try: self.sock.sendto(msg, d['addr'])
                    except: pass


# ott_node.py (Parte 2: Processamento e Lógica de Roteamento)

    def processar_pacote(self, data, addr):
        try:
            mensagem = data.decode()
            
            # Tenta deserializar como LSA (JSON) primeiro, se falhar, tenta PING/PONG/JOIN/STREAM
            try:
                msg_dict = json.loads(mensagem)
                msg_type = msg_dict.get('type')
                
                if msg_type == 'LSA':
                    plano_contolo.process_lsa(msg_dict, addr, self._send_udp_internal)
                    return
            except json.JSONDecodeError:
                # Não é um LSA (é um PING, PONG, JOIN ou STREAM)
                pass
            
            # 1. PING (Resposta)
            if mensagem.startswith("PING"):
                # ... (O seu código PING/PONG permanece o mesmo, respondendo imediatamente)
                _, origem, seq, ts_envio = mensagem.split()
                resp = f"PONG {self.node_name} {ts_envio}".encode()
                self.sock.sendto(resp, addr)

            # 2. PONG (Recálculo da Latência e Perda)
            elif mensagem.startswith("PONG"):
                _, origem, ts_envio_str = mensagem.split()
                rtt = (time.time() - float(ts_envio_str)) * 1000 # Em ms
                latencia_calculada = rtt / 2
                
                with NODE_STATE.lock_vizinhos:
                    if origem in NODE_STATE.tabela_vizinhos:
                        d = NODE_STATE.tabela_vizinhos[origem]
                        d['addr'] = addr
                        d['last_seen'] = time.time()
                        d['pongs_recebidos'] = d.get('pongs_recebidos', 0) + 1
                        
                        # Média móvel para suavizar a latência
                        antiga = d['latencia']
                        d['latencia'] = (antiga * 0.7) + (latencia_calculada * 0.3)
                        
            # 3. JOIN (Adesão à Stream)
            elif mensagem.startswith("JOIN"):
                partes = mensagem.split(" ")
                stream_id = partes[1]
                
                vizinho_nome = self._get_neighbor_by_addr(addr)
                
                if vizinho_nome:
                    print(f"[{self.node_name}]  Pedido JOIN de {vizinho_nome} para {stream_id}")
                    
                    # Adiciona à lista downstream
                    with NODE_STATE.lock_fluxos:
                        if stream_id not in NODE_STATE.fluxos:
                            NODE_STATE.fluxos[stream_id] = {'downstream': []}
                        if vizinho_nome not in NODE_STATE.fluxos[stream_id]['downstream']:
                            NODE_STATE.fluxos[stream_id]['downstream'].append(vizinho_nome)

                    # Se sou a FONTE, começo a enviar. Se não, peço para cima.
                    if self.node_name == stream_id:
                        print(f"[{self.node_name}] 🎬 SOU A FONTE! Cliente ligado.")
                    else:
                        self.enviar_pedido_join_upstream(stream_id)
                        
            # 4. STREAM (Dados de Vídeo)
            elif mensagem.startswith("STREAM"):
                partes = mensagem.split(" ", 2)
                if len(partes) >= 2:
                    stream_id = partes[1]
                    self.reencaminhar_dados(stream_id, data)

        except Exception as e:
            # print(f"Erro processar: {e}") 
            pass

    def enviar_pedido_join_upstream(self, stream_id):
        """
        Substitui a lógica de Tabela de Rotas DV pelo cálculo A*.
        Calcula o caminho de menor latência/perda (custo) e envia JOIN.
        """
        
        # 1. Executa o algoritmo A* (Dijkstra)
        proximo_salto, custo_total = plano_contolo.calculate_a_star_route(stream_id, self.node_name)
        
        if proximo_salto and proximo_salto != 'SELF':
            
            with NODE_STATE.lock_vizinhos:
                target_info = NODE_STATE.tabela_vizinhos.get(proximo_salto)
            
            if target_info:
                addr = target_info['addr']
                
                # Opcional: Cache da rota
                with NODE_STATE.lock_rotas_cache:
                    NODE_STATE.tabela_rotas_cache[stream_id] = {
                        'proximo_salto': proximo_salto,
                        'custo_total': custo_total
                    }
                    
                print(f"[{self.node_name}] ⬆️ A* Rota: {proximo_salto} (Custo: {custo_total:.1f}ms). Enviando JOIN...")
                
                try:
                    self.sock.sendto(f"JOIN {stream_id}".encode(), addr)
                except Exception as e:
                    print(f"[{self.node_name}]  Erro socket ao enviar JOIN: {e}")
            else:
                print(f"[{self.node_name}] Erro: Próximo salto {proximo_salto} não encontrado na tabela de vizinhos.")
        else:
            print(f"[{self.node_name}] Erro: Não foi encontrada rota para {stream_id}. Aguardando LSA...")

    # Função auxiliar para enviar UDP a partir dos módulos
    def _send_udp_internal(self, ip, port, message_bytes):
        self.sock.sendto(message_bytes, (ip, port))

    def reencaminhar_dados(self, stream_id, pacote_bruto):
        # ... (O seu código reencaminhar_dados permanece o mesmo, usando NODE_STATE.fluxos)
        pass

# ott_node.py (Parte 3: Lançamento)

    def start(self):
        if not self.contactar_bootstrapper(): return

        # Threads comuns a todos
        threading.Thread(target=self.thread_ouvir, daemon=True).start()
        threading.Thread(target=self.thread_medir_latencia, daemon=True).start()
        
        # ADICIONAR: Thread de Anúncio LSA (para todos os nós)
        threading.Thread(target=plano_contolo.thread_lsa_anuncio, 
                 args=(self._send_udp_internal,), 
                 daemon=True).start()

        # Threads Específicas
        if self.node_name.startswith("STREAMER"):
            # REMOVER: thread_servidor_anunciar (substituída pela thread_lsa_anuncio)
            threading.Thread(target=self.thread_gerar_video, daemon=True).start()
        
        if self.node_name.startswith("C"):
            threading.Thread(target=self.thread_menu, daemon=True).start()

        print(f"[{self.node_name}] Operacional.")
        try:
            while NODE_STATE.running: time.sleep(1)
        except: self.stop()

    def stop(self):
        NODE_STATE.running = False
        self.sock.close()
        print("Bye.")

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Uso: python3 ott_node.py <NOME> <IP> <PORTA>")
        sys.exit(1)
    OTTNode(sys.argv[1], sys.argv[2], sys.argv[3]).start()