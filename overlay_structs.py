import struct
import json
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Dict, Set, Optional

# --- CONSTANTES ---
MAX_PACKET_SIZE = 4096 
HEADER_FORMAT = "!B 16s 16s I d" # Type, SrcIP, DstIP, Seq, Timestamp
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

class MsgType(Enum):
    HELLO = 1            
    ROUTE_DISCOVERY = 2  # Flood (LSA)
    ROUTE_REPLY = 3      # Reservado
    STREAM_JOIN = 4      # Cliente pede stream
    STREAM_DATA = 5      # Dados do video
    HELLO_RESPONSE = 6   # Para medir RTT (Pong)
    STREAM_LEAVE = 7     # Para parar o stream
    STREAM_FEC = 8       # Pacote de Correção de Erro (Paridade)
    STREAM_REPORT = 9    # Relatório de Qualidade (Feedback do Cliente)
    ACK_JOIN = 10        # Confirmação de JOIN para fiabilidade
    DEBUG = 99

@dataclass
class RouteEntry:
    source_id: str          
    proximo_salto_ip: str   
    custo_acumulado: float  
    downstream_ips: Set[str] = field(default_factory=set)
    last_update: float = 0.0

class OverlayNode:
    def __init__(self, node_id, ip, port):
        self.node_id = node_id
        self.ip = ip
        self.port = port
        self.sequence_number = 0
        # neighbors: ip -> { metric: float, last_seen: float, state: 'alive'|'lost'|'dead' }
        self.neighbors: Dict[str, Dict] = {}
        self.routing_table: Dict[str, RouteEntry] = {}
        # lsa_database maps (stream_id, origin_seq) -> metadata dict
        # metadata: { 'time': float, 'data': dict, 'sender': str, 'cost': float }
        self.lsa_database: Dict[tuple, dict] = {}
        self.pending_pings: Dict[int, float] = {}
    
    def check_dead_neighbors(self, timeout=20.0):
        """Verifica estado dos vizinhos e remove/retorna os mortos.

        Comportamento:
        - Se `time_since_seen` < timeout/2 => `alive`
        - Se timeout/2 <= `time_since_seen` < timeout => `lost`
        - Se `time_since_seen` >= timeout => `dead` (removido)

        Retorna lista de vizinhos removidos (dead).
        """
        now = time.time()
        dead_neighbors = []

        # thresholds
        dead_threshold = timeout
        lost_threshold = timeout / 2.0

        for n_ip, info in list(self.neighbors.items()):
            last_seen = info.get('last_seen', 0)
            if last_seen == 0:
                # recém descoberto
                self.neighbors[n_ip]['state'] = 'alive'
                continue

            time_since_seen = now - last_seen
            prev_state = info.get('state', 'alive')

            if time_since_seen < lost_threshold:
                self.neighbors[n_ip]['state'] = 'alive'
            elif time_since_seen < dead_threshold:
                self.neighbors[n_ip]['state'] = 'lost'
                if prev_state != 'lost':
                    print(f"[{self.node_id}] ⚠️ Vizinho {n_ip} marcado como LOST (sem resposta {time_since_seen:.1f}s)")
            else:
                # marcar como dead e remover
                self.neighbors[n_ip]['state'] = 'dead'
                dead_neighbors.append(n_ip)
                print(f"[{self.node_id}] ☠️ Vizinho {n_ip} considerado DEAD (sem resposta {time_since_seen:.1f}s)")

        if dead_neighbors:
            # Remover dead neighbors e tentar recalcular rotas afetadas
            for dead_ip in dead_neighbors:
                del self.neighbors[dead_ip]

                routes_removed = []
                for stream_id, entry in list(self.routing_table.items()):
                    if entry.proximo_salto_ip == dead_ip:
                        # Procurar alternativas nas LSAs conhecidas para este stream
                        candidates = []
                        for lsa_key, lsa_info in self.lsa_database.items():
                            sid, origin_seq = lsa_key
                            if sid != stream_id:
                                continue
                            sender = lsa_info.get('sender')
                            cost = lsa_info.get('cost', float('inf'))
                            lsa_time = lsa_info.get('time', 0)
                            lsa_age = time.time() - lsa_time
                            
                            # MODO AGRESSIVO: tentar TODAS as LSAs disponíveis
                            # Prioridade 1: vizinhos vivos
                            # Prioridade 2: LSAs recentes (< dead_threshold)
                            # Prioridade 3: LSAs antigas mas ainda válidas (< 60s)
                            ninfo = self.neighbors.get(sender)
                            
                            if ninfo and ninfo.get('state') == 'alive':
                                # Vizinho vivo - prioridade máxima
                                candidates.append((cost, sender, origin_seq, 0))  # prioridade 0 (melhor)
                            elif lsa_age < dead_threshold:
                                # LSA recente - boa prioridade
                                candidates.append((cost, sender, origin_seq, 1))
                            elif lsa_age < 60.0:
                                # LSA antiga mas ainda pode funcionar - última tentativa
                                penalty = cost * 0.2  # penalidade de 20%
                                candidates.append((cost + penalty, sender, origin_seq, 2))
                            # Se nenhuma das condições, não adicionar (LSA muito antiga)

                        if candidates:
                            # Ordenar por prioridade primeiro, depois por custo
                            candidates.sort(key=lambda x: (x[3], x[0]))  # (prioridade, custo)
                            best_cost, best_sender, best_origin, priority = candidates[0]
                            priority_label = ['vizinho vivo', 'LSA recente', 'LSA antiga'][priority]
                            self.routing_table[stream_id] = RouteEntry(stream_id, best_sender, best_cost)
                            self.routing_table[stream_id].last_update = time.time()
                            print(f"[{self.node_id}] 🔁 Rota para {stream_id} recalculada: via {best_sender} (custo {best_cost:.2f}, {priority_label})")
                            if len(candidates) > 1:
                                print(f"[{self.node_id}]    └─ {len(candidates)-1} alternativa(s) disponível(eis)")
                        else:
                            print(f"[{self.node_id}] ❌ REMOVENDO rota para {stream_id} (next hop {dead_ip} morto)")
                            routes_removed.append(stream_id)
                            del self.routing_table[stream_id]

                if routes_removed:
                    # NÃO limpar TODAS as LSAs - apenas as do vizinho morto específico!
                    # Isso mantém LSAs de caminhos alternativos para failover futuro
                    lsas_deleted = 0
                    for sid in routes_removed:
                        for k in list(self.lsa_database.keys()):
                            lsa_stream_id, lsa_seq = k
                            lsa_sender = self.lsa_database[k].get('sender')
                            # Só deletar LSAs deste stream que vieram do vizinho morto
                            if lsa_stream_id == sid and lsa_sender == dead_ip:
                                del self.lsa_database[k]
                                lsas_deleted += 1
                    print(f"[{self.node_id}] 🧹 Removidas {lsas_deleted} LSAs do vizinho morto (mantendo alternativas)")
                    print(f"[{self.node_id}] 📊 LSAs restantes: {len(self.lsa_database)} (podem conter rotas alternativas)")

        return dead_neighbors

    def pack_message(self, msg_type: MsgType, dest_ip: str, payload: bytes = b"") -> bytes:
        self.sequence_number += 1
        timestamp = time.time()
        src_ip_bytes = self.ip.encode('utf-8').ljust(16, b'\0')
        dest_ip_bytes = dest_ip.encode('utf-8').ljust(16, b'\0')
        header = struct.pack(HEADER_FORMAT, msg_type.value, src_ip_bytes, dest_ip_bytes, self.sequence_number, timestamp)
        return header + payload

    def unpack_message(self, data: bytes):
        if len(data) < HEADER_SIZE: return None, None
        header_bytes = data[:HEADER_SIZE]
        payload = data[HEADER_SIZE:]
        msg_type_val, src_ip_raw, dst_ip_raw, seq, ts = struct.unpack(HEADER_FORMAT, header_bytes)
        return {
            "type": MsgType(msg_type_val),
            "source_ip": src_ip_raw.decode('utf-8').strip('\0'),
            "dest_ip": dst_ip_raw.decode('utf-8').strip('\0'),
            "seq": seq,
            "timestamp": ts
        }, payload

    # --- LÓGICA DE PROTOCOLO ---

    def handle_hello(self, header, sender_ip):
        now = time.time()
        # Modo Estrito: Só responder se for vizinho conhecido
        if sender_ip in self.neighbors:
            # SEMPRE atualizar last_seen quando recebe HELLO
            self.neighbors[sender_ip]['last_seen'] = now
            self.neighbors[sender_ip]['state'] = 'alive'
            return json.dumps({"ack_seq": header['seq']}).encode('utf-8')
        return b""

    def handle_hello_response(self, payload, sender_ip):
        now = time.time()
        try:
            data = json.loads(payload.decode('utf-8'))
            ack_seq = data['ack_seq']
            if ack_seq in self.pending_pings:
                start_time = self.pending_pings.pop(ack_seq)
                rtt_ms = (now - start_time) * 1000.0 
                
                # CRÍTICO: Atualizar last_seen sempre que recebemos resposta
                if sender_ip in self.neighbors:
                    old_metric = self.neighbors[sender_ip].get('metric', rtt_ms)
                    new_metric = (0.7 * old_metric) + (0.3 * rtt_ms)
                    self.neighbors[sender_ip]['metric'] = new_metric
                    self.neighbors[sender_ip]['last_seen'] = now  #FIX!
                    self.neighbors[sender_ip]['state'] = 'alive'
        except: pass


    def handle_flood(self, header, payload, sender_ip_real):
        try:
            data = json.loads(payload.decode('utf-8'))
            stream_id = data['stream_id']
            custo_recebido = data['cost']
            origin_seq = data['origin_seq']
        except: return None

        # DESCOBERTA DINÂMICA: aprender vizinhos através de floods recebidos.
        # Quando recebe flood de IP desconhecido, registar como vizinho e marcar timestamp.
        if sender_ip_real not in self.neighbors:
            print(f"[{self.node_id}] 🔍 Descoberto novo vizinho via FLOOD: {sender_ip_real}")
            self.neighbors[sender_ip_real] = {
                'metric': 50.0, 
                'last_seen': time.time(), 
                'state': 'alive', 
                'missed_hellos': 0,
                'discovered_via': 'flood'  # Marcar origem da descoberta
            }

        # Verificar se vizinho está respondendo aos HELLOs (verificação leve)
        neighbor_info = self.neighbors.get(sender_ip_real, {})
        time_since_seen = time.time() - neighbor_info.get('last_seen', 0)
        # Aceitar floods mesmo de vizinhos que não respondem (broadcast),
        # mas dar preferência a rotas de vizinhos ativos
        if time_since_seen > 60.0:  # Muito tempo sem resposta (1 minuto)
            # Silenciosamente ignorar (pode ser broadcast de nó distante)
            return None

        # Verificar duplicados (Loop prevention) - MAS permitir se não temos rota
        lsa_key = (stream_id, origin_seq)
        if lsa_key in self.lsa_database:
            # Se já temos rota válida, ignorar duplicado
            if stream_id in self.routing_table:
                return None
            # Se não temos rota, aceitar mesmo que seja duplicado (recuperação)
            print(f"[{self.node_id}] 🔓 Aceitando FLOOD duplicado (sem rota válida)")

        # Calcular custo atualizado e guardar metadados do LSA para recomputação
        metric_link = self.neighbors[sender_ip_real].get('metric', 50.0)
        novo_custo = custo_recebido + metric_link

        self.lsa_database[lsa_key] = {
            'time': time.time(),
            'data': data,
            'sender': sender_ip_real,
            'cost': novo_custo
        }

        # DEBUG: Confirmar recepção de flood novo
        print(f"[{self.node_id}] 📡 Flood recebido: {stream_id} de {sender_ip_real} (custo={novo_custo:.2f})")

        # --- LÓGICA DE ROTEAMENTO DINÂMICO ---
        should_propagate = False
        route_changed = False

        if stream_id not in self.routing_table:
            # Rota nova: Aceitar sempre
            self.routing_table[stream_id] = RouteEntry(stream_id, sender_ip_real, novo_custo)
            self.routing_table[stream_id].last_update = time.time()
            should_propagate = True
            route_changed = True
            print(f"[{self.node_id}] 🆕 Nova rota para {stream_id}: via {sender_ip_real} (custo {novo_custo:.2f})")
        else:
            rota = self.routing_table[stream_id]
            old_next_hop = rota.proximo_salto_ip
            old_cost = rota.custo_acumulado
            
            # CASO 1: Encontrámos um caminho SIGNIFICATIVAMENTE MELHOR
            if novo_custo < old_cost * 0.90:  # 10% melhor
                print(f"[{self.node_id}] 🔄 Caminho MELHOR para {stream_id}: {sender_ip_real} (custo {novo_custo:.2f} << {old_cost:.2f})")
                rota.proximo_salto_ip = sender_ip_real
                rota.custo_acumulado = novo_custo
                rota.last_update = time.time()
                should_propagate = True
                route_changed = True
            
            # CASO 2: Atualização do caminho ATUAL (mesmo Next Hop)
            elif sender_ip_real == old_next_hop:
                # Sempre aceitar atualizações do next hop atual
                if abs(novo_custo - old_cost) > 1.0:  # Mudança significativa
                    if novo_custo > old_cost:
                        print(f"[{self.node_id}] ⚠️ Caminho PIOROU para {stream_id}: custo {novo_custo:.2f} (era {old_cost:.2f})")
                    else:
                        print(f"[{self.node_id}] ✅ Caminho melhorou para {stream_id}: custo {novo_custo:.2f} (era {old_cost:.2f})")
                rota.custo_acumulado = novo_custo
                rota.last_update = time.time()
                should_propagate = True
                # Não marcamos route_changed porque o next_hop é o mesmo
            
            # CASO 3: Caminho alternativo bom (pode ser útil para failover)
            elif novo_custo < old_cost * 1.20:  # Até 20% pior que o atual
                # Não mudamos a rota, mas propagamos para dar visibilidade
                print(f"[{self.node_id}] 🔀 Caminho ALTERNATIVO para {stream_id}: via {sender_ip_real} (custo {novo_custo:.2f} vs {old_cost:.2f})")
                should_propagate = True

        if should_propagate:
            data['cost'] = novo_custo
            # Marca se houve mudança de next hop para downstream nodes saberem
            if route_changed:
                data['route_changed'] = True
            print(f"[{self.node_id}] ➡️ Propagando flood {stream_id} (custo {novo_custo:.2f})")
            time.sleep(0.002)  # Pequeno jitter
            return json.dumps(data).encode('utf-8')
        else:
            print(f"[{self.node_id}] 🚫 NÃO propagando {stream_id} (custo {novo_custo:.2f} não melhora {self.routing_table.get(stream_id, RouteEntry('', '', float('inf'))).custo_acumulado:.2f})")
            
        return None
    
    def handle_join(self, payload, sender_ip_real):
        """
        Retorna: (upstream_ip, send_ack, already_receiving)
        - upstream_ip: para onde propagar o JOIN (None se não propagar, "SOURCE" se sou a fonte)
        - send_ack: se deve enviar ACK ao sender
        - already_receiving: True se já estou recebendo este stream (não preciso propagar)
        """
        try:
            data = json.loads(payload.decode('utf-8'))
            target_stream = data['stream_id']
        except: return None, False, False

        # Se sou Streamer (SOURCE)
        if self.node_id == target_stream:
            if target_stream not in self.routing_table:
                self.routing_table[target_stream] = RouteEntry(target_stream, "SELF", 0.0)
                print(f"[{self.node_id}] 📝 Entrada de roteamento criada para SELF")
            entry = self.routing_table[target_stream]
            was_first = len(entry.downstream_ips) == 0
            if sender_ip_real not in entry.downstream_ips:
                entry.downstream_ips.add(sender_ip_real)
                print(f"[{self.node_id}] 🎬 NOVO CLIENTE: {sender_ip_real} (total: {len(entry.downstream_ips)})")
            else:
                print(f"[{self.node_id}] ⚠️ Cliente já existe: {sender_ip_real}")
            return "SOURCE", True, True  # Sou a fonte, já estou "recebendo" (gerando)

        # Se sou Router e JÁ tenho rota para este stream
        if target_stream in self.routing_table:
            entry = self.routing_table[target_stream]
            was_first = len(entry.downstream_ips) == 0
            if sender_ip_real not in entry.downstream_ips:
                entry.downstream_ips.add(sender_ip_real)
                print(f"[{self.node_id}] 🔌 Cliente adicionado: {sender_ip_real} (total: {len(entry.downstream_ips)})")
                if was_first:
                    print(f"[{self.node_id}]    └─ Primeiro cliente! Preciso propagar JOIN upstream")
                else:
                    print(f"[{self.node_id}]    └─ Já estou recebendo stream, apenas adicionei aos downstreams")
            return entry.proximo_salto_ip, True, (not was_first)  # already_receiving = True se não era o primeiro
        
        # Não tenho rota para este stream
        return None, False, False

    def handle_leave(self, payload, sender_ip_real):
        try:
            data = json.loads(payload.decode('utf-8'))
            target_stream = data['stream_id']
        except: return None

        if target_stream in self.routing_table:
            entry = self.routing_table[target_stream]
            if sender_ip_real in entry.downstream_ips:
                entry.downstream_ips.remove(sender_ip_real)
                print(f"[{self.node_id}] ✂️ Cliente saiu: {sender_ip_real}")
            
            if len(entry.downstream_ips) == 0 and self.node_id != target_stream:
                print(f"[{self.node_id}] 🍂 Sem clientes. Pedindo corte ao upstream.")
                return entry.proximo_salto_ip
        return None

    def handle_report(self, payload, sender_ip_real):
        try:
            data = json.loads(payload.decode('utf-8'))
            target_stream = data['stream_id']
            packet_loss = data.get('loss_rate', 0.0)
            client_id = data.get('client_id', 'unknown')
        except: return None

        if self.node_id == target_stream:
            print(f"[{self.node_id}] 📊 Relatório {client_id}: Perda {packet_loss:.1f}%")
            return "SOURCE"
        
        if target_stream in self.routing_table:
            entry = self.routing_table[target_stream]
            return entry.proximo_salto_ip
        return None

    def handle_debug(self, header, payload, sender_ip_real):
        """Handler minimal para pedidos debug/consulta de rotas.

        Payload esperado: JSON {"cmd": "route_request"}
        Resposta: JSON {'routes': { stream_id: { 'cost': float } }}
        """
        try:
            data = json.loads(payload.decode('utf-8'))
            cmd = data.get('cmd')
        except:
            return None

        if cmd == 'route_request':
            routes = {}
            for sid, entry in self.routing_table.items():
                routes[sid] = {'cost': entry.custo_acumulado}
            return json.dumps({'routes': routes}).encode('utf-8')
        return None