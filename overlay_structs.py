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
        self.neighbors: Dict[str, Dict] = {}
        self.routing_table: Dict[str, RouteEntry] = {}
        self.lsa_database: Dict[tuple, float] = {}
        self.pending_pings: Dict[int, float] = {}
    
    def check_dead_neighbors(self, timeout=10.0):
        """Verifica vizinhos mortos e remove rotas que dependem deles"""
        now = time.time()
        dead_neighbors = []
        dead_info = {}  # Para guardar info de debug
        
        for n_ip, info in list(self.neighbors.items()):
            last_seen = info.get('last_seen', 0)
            # Só verifica se já vimos este vizinho (last_seen > 0)
            if last_seen == 0:
                continue  # Vizinho recém descoberto, dar tempo
            
            time_since_seen = now - last_seen
            # Considera morto se passou MUITO tempo sem resposta
            if time_since_seen > timeout:
                dead_neighbors.append(n_ip)
                dead_info[n_ip] = time_since_seen
        
        if dead_neighbors:
            for dead_ip in dead_neighbors:
                print(f"[{self.node_id}] ☠️ Vizinho MORTO: {dead_ip} (sem resposta há {dead_info[dead_ip]:.1f}s)")
                del self.neighbors[dead_ip]
                
                # REMOVER rotas que usam esse vizinho como next hop
                routes_removed = []
                for stream_id, entry in list(self.routing_table.items()):
                    if entry.proximo_salto_ip == dead_ip:
                        print(f"[{self.node_id}] ❌ REMOVENDO rota para {stream_id} (next hop {dead_ip} morto)")
                        routes_removed.append(stream_id)
                        del self.routing_table[stream_id]
                
                # LIMPAR TODAS AS LSAs para permitir re-flood
                if routes_removed:
                    print(f"[{self.node_id}] 🧹 LIMPANDO TODAS as LSAs para permitir nova descoberta")
                    self.lsa_database.clear()
                    print(f"[{self.node_id}] 🔄 {len(routes_removed)} rota(s) removida(s), PRONTO para novos FLOODs")
        
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
            self.neighbors[sender_ip]['last_seen'] = now
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
        except: pass

    '''def handle_flood(self, header, payload, sender_ip_real):
        try:
            data = json.loads(payload.decode('utf-8'))
            stream_id = data['stream_id']
            custo_recebido = data['cost']
            origin_seq = data['origin_seq']
        except: return None

        # Modo Estrito: Ignorar flood de desconhecidos
        if sender_ip_real not in self.neighbors: return None

        lsa_key = (stream_id, origin_seq)
        if lsa_key in self.lsa_database: return None
        self.lsa_database[lsa_key] = time.time()

        metric_link = self.neighbors[sender_ip_real]['metric']
        novo_custo = custo_recebido + metric_link

        melhorou = False
        if stream_id not in self.routing_table:
            self.routing_table[stream_id] = RouteEntry(stream_id, sender_ip_real, novo_custo)
            melhorou = True
        else:
            rota = self.routing_table[stream_id]
            if novo_custo < rota.custo_acumulado:
                rota.proximo_salto_ip = sender_ip_real
                rota.custo_acumulado = novo_custo
                melhorou = True

        if melhorou:
            data['cost'] = novo_custo
            return json.dumps(data).encode('utf-8')
        return None '''

    def handle_flood(self, header, payload, sender_ip_real):
        try:
            data = json.loads(payload.decode('utf-8'))
            stream_id = data['stream_id']
            custo_recebido = data['cost']
            origin_seq = data['origin_seq']
        except: return None

        # Modo Estrito: Ignorar flood de desconhecidos
        if sender_ip_real not in self.neighbors:
            print(f"[{self.node_id}] ⚠️ FLOOD ignorado: {sender_ip_real} não é vizinho")
            return None
        
        # Verificar se vizinho está respondendo aos HELLOs (verificação leve)
        neighbor_info = self.neighbors[sender_ip_real]
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
        self.lsa_database[lsa_key] = time.time()
        
        # DEBUG: Confirmar recepção de flood novo
        print(f"[{self.node_id}] 📡 Flood recebido: {stream_id} de {sender_ip_real} (custo={custo_recebido:.2f})")

        metric_link = self.neighbors[sender_ip_real]['metric']
        novo_custo = custo_recebido + metric_link

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
        Retorna: (upstream_ip, send_ack)
        """
        try:
            data = json.loads(payload.decode('utf-8'))
            target_stream = data['stream_id']
        except: return None, False

        # Se sou Streamer
        if self.node_id == target_stream:
            if target_stream not in self.routing_table:
                self.routing_table[target_stream] = RouteEntry(target_stream, "SELF", 0.0)
                print(f"[{self.node_id}] 📝 Entrada de roteamento criada para SELF")
            entry = self.routing_table[target_stream]
            if sender_ip_real not in entry.downstream_ips:
                entry.downstream_ips.add(sender_ip_real)
                print(f"[{self.node_id}] 🎬 NOVO CLIENTE: {sender_ip_real} (total: {len(entry.downstream_ips)})")
            else:
                print(f"[{self.node_id}] ⚠️ Cliente já existe: {sender_ip_real}")
            return "SOURCE", True # True = Enviar ACK

        # Se sou Router
        if target_stream in self.routing_table:
            entry = self.routing_table[target_stream]
            if sender_ip_real not in entry.downstream_ips:
                entry.downstream_ips.add(sender_ip_real)
                print(f"[{self.node_id}] 🔌 Cliente adicionado: {sender_ip_real}")
            return entry.proximo_salto_ip, True # True = Enviar ACK
        
        return None, False

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