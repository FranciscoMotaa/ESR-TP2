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
    ROUTE_REPLY = 3      
    STREAM_JOIN = 4      
    STREAM_DATA = 5      
    HELLO_RESPONSE = 6   # Para medir RTT (Pong)
    STREAM_LEAVE = 7     
    STREAM_FEC = 8       
    STREAM_REPORT = 9    # Relatório de Qualidade (Feedback do Cliente)
    ACK_JOIN = 10        
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
        # Incluímos 'loss_rate' no dict de vizinhos, default 0.0
        self.neighbors: Dict[str, Dict] = {} 
        self.routing_table: Dict[str, RouteEntry] = {}
        # lsa_database armazena (stream_id, origin_seq) para evitar loops
        self.lsa_database: Dict[tuple, float] = {} 
        self.pending_pings: Dict[int, float] = {}
        
        # Parâmetros de Roteamento
        self.LOSS_WEIGHT = 50.0  # 1% Loss = 50ms Latency Penalty
        self.CHANGE_THRESHOLD = 0.05 # Histerese de 5%

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
        if sender_ip in self.neighbors:
            self.neighbors[sender_ip]['last_seen'] = now
            return json.dumps({"ack_seq": header['seq']}).encode('utf-8')
        return b""

    def handle_hello_response(self, payload, sender_ip):
        try:
            data = json.loads(payload.decode('utf-8'))
            ack_seq = data['ack_seq']
            if ack_seq in self.pending_pings:
                start_time = self.pending_pings.pop(ack_seq)
                rtt_ms = (time.time() - start_time) * 1000.0 
                
                # Inicializa ou atualiza métrica de vizinho
                if sender_ip not in self.neighbors:
                    self.neighbors[sender_ip] = {'metric': rtt_ms, 'last_seen': time.time(), 'loss_rate': 0.0}
                else:
                    old_metric = self.neighbors[sender_ip].get('metric', rtt_ms)
                    new_metric = (0.7 * old_metric) + (0.3 * rtt_ms) # Moving Average
                    self.neighbors[sender_ip]['metric'] = new_metric
                    self.neighbors[sender_ip]['last_seen'] = time.time()
        except: pass

    # Incluímos o método de limpeza para robustez
    def cleanup_neighbors(self, expiry_time):
        """Remove vizinhos que não respondem ao HELLO."""
        now = time.time()
        to_remove = []
        for n_ip, info in list(self.neighbors.items()):
            if now - info.get('last_seen', 0) > expiry_time:
                print(f"[⚠️] Vizinho {n_ip} expirado, a remover.")
                to_remove.append(n_ip)
        
        for n_ip in to_remove:
            del self.neighbors[n_ip]
            # O Flood irá garantir o re-roteamento, mas limpamos a rota se o next_hop falhar
            for sid, entry in list(self.routing_table.items()):
                if entry.proximo_salto_ip == n_ip:
                    print(f"[⚠️] Rota para {sid} via {n_ip} inválida. Limpeza.")
                    del self.routing_table[sid] # Força re-descoberta no próximo Flood
    
    # --- MÉTODO CRÍTICO: ROTEAMENTO COM PERDA E HISTERESE ---
    def handle_flood(self, header, payload, sender_ip_real):
        try:
            data = json.loads(payload.decode('utf-8'))
            stream_id = data['stream_id']
            custo_recebido = data['cost']
            origin_seq = data['origin_seq']
        except: return None

        if sender_ip_real not in self.neighbors: return None

        lsa_key = (stream_id, origin_seq)
        if lsa_key in self.lsa_database: return None
        self.lsa_database[lsa_key] = time.time()

        # 1. CÁLCULO DA MÉTRICA DE LIGAÇÃO (Latência + Perda Ponderada)
        metric_link = self.neighbors[sender_ip_real].get('metric', 1000)
        # Buscar a taxa de perda (se o vizinho for downstream e tiver reportado)
        link_loss = self.neighbors[sender_ip_real].get('loss_rate', 0.0) 
        
        # Ponderação: 1% de perda = 50ms de latência. 
        # Custo do link = Latência + Penalização por Perda
        custo_do_link = metric_link + (link_loss * self.LOSS_WEIGHT) 
        
        novo_custo = custo_recebido + custo_do_link 
        
        # --- LÓGICA DE ROTAS COM HISTERESE ---
        should_propagate = False
        
        current_cost = -1.0
        old_next_hop = "N/A"
        
        if stream_id not in self.routing_table:
            # Rota nova: Aceitar sempre
            self.routing_table[stream_id] = RouteEntry(stream_id, sender_ip_real, novo_custo)
            print(f"[{self.node_id}] ➕ ROTA NOVA para {stream_id}. Próximo Salto: {sender_ip_real} (Custo: {novo_custo:.2f})")
            should_propagate = True
        else:
            rota = self.routing_table[stream_id]
            current_cost = rota.custo_acumulado
            old_next_hop = rota.proximo_salto_ip

            # CASO 1: Encontrámos um caminho SIGNIFICATIVAMENTE MELHOR (HANDOVER)
            if novo_custo < current_cost * (1 - self.CHANGE_THRESHOLD):
                
                # --- LOGGING CLARO DE MUDANÇA DE ROTA (HANDOVER) ---
                print(f"[{self.node_id}] 🔄 HANDOVER para {stream_id}!")
                print("--- Tabela de Rotas Atualizada ---")
                print("| ROTA | CAMINHO ANTIGO | CUSTO ANTIGO | CAMINHO NOVO | CUSTO NOVO |")
                print("| :--- | :------------- | :----------- | :----------- | :--------- |")
                print(f"| {stream_id:<4} | {old_next_hop:<14} | {current_cost:<12.2f} | {sender_ip_real:<12} | {novo_custo:<10.2f} |")
                print("-----------------------------------")
                
                rota.proximo_salto_ip = sender_ip_real
                rota.custo_acumulado = novo_custo
                should_propagate = True
            
            # CASO 2: O caminho ATUAL piorou
            elif sender_ip_real == old_next_hop and novo_custo > current_cost * (1 + self.CHANGE_THRESHOLD):
                 print(f"[{self.node_id}] ⚠️  Caminho atual PIOROU para {stream_id}: custo {novo_custo:.2f} (era {current_cost:.2f})")
                 rota.custo_acumulado = novo_custo
                 should_propagate = True
            
            # CASO 3: Caminho alternativo que é competitivo (Propagar)
            elif novo_custo <= current_cost * (1 + self.CHANGE_THRESHOLD * 2):
                should_propagate = True

        if should_propagate:
            data['cost'] = novo_custo
            time.sleep(0.005) 
            return json.dumps(data).encode('utf-8')
            
        return None
    
    # --- NOVO MÉTODO: DIAGNÓSTICO DA ÁRVORE ---
    def diagnose_multicast_tree(self) -> str:
        """
        Retorna uma representação da sub-árvore de multicast que este nó serve.
        """
        output = [f"--- Árvore de Multicast ({self.node_id}) ---"]
        
        streams_servidos = [sid for sid, entry in self.routing_table.items() 
                            if len(entry.downstream_ips) > 0 or sid == self.node_id]
        
        if not streams_servidos:
            output.append("Nenhum stream ativo ou a ser servido.")
            return "\n".join(output)

        for sid in streams_servidos:
            entry = self.routing_table[sid]
            
            # Raiz da Sub-Árvore
            role = "SOURCE" if sid == self.node_id else "ROUTER/CACHE"
            upstream_info = f"via {entry.proximo_salto_ip} (Custo: {entry.custo_acumulado:.2f})" if sid != self.node_id else "(Origem)"
            output.append(f"\n[{sid}] ({role}) {upstream_info}")
            
            if entry.downstream_ips:
                output.append("  Clientes Downstream:")
                for i, client_ip in enumerate(sorted(entry.downstream_ips)):
                    # Adiciona a métrica do link (RTT + Penalização de Perda)
                    link_info = self.neighbors.get(client_ip, {})
                    rtt = link_info.get('metric', 'N/A')
                    loss = link_info.get('loss_rate', 'N/A')
                    
                    link_str = f"Link: {rtt:.1f}ms RTT / {loss:.1f}% Loss" if isinstance(rtt, float) else "Link: N/A"
                    
                    prefixo = "└── " if i == len(entry.downstream_ips) - 1 else "├── "
                    output.append(f"  {prefixo}{client_ip} ({link_str})")
            else:
                output.append("  Nenhum cliente Downstream direto.")
                
        return "\n".join(output)
        
    # --- LÓGICA DA ÁRVORE DE MULTICAST (JOIN/LEAVE) ---

    def handle_join(self, payload, sender_ip_real):
        try:
            data = json.loads(payload.decode('utf-8'))
            target_stream = data['stream_id']
        except: return None, False

        if self.node_id == target_stream:
            if target_stream not in self.routing_table:
                self.routing_table[target_stream] = RouteEntry(target_stream, "SELF", 0.0)
            entry = self.routing_table[target_stream]
            if sender_ip_real not in entry.downstream_ips:
                entry.downstream_ips.add(sender_ip_real)
                print(f"[{self.node_id}] 🎬 NOVO CLIENTE: {sender_ip_real}")
            return "SOURCE", True 

        if target_stream in self.routing_table:
            entry = self.routing_table[target_stream]
            if sender_ip_real not in entry.downstream_ips:
                entry.downstream_ips.add(sender_ip_real)
                print(f"[{self.node_id}] 🔌 Cliente adicionado: {sender_ip_real}")
            return entry.proximo_salto_ip, True 
        
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

        # CRÍTICO: Armazenar a perda do link com o vizinho DOWNSTREAM
        if sender_ip_real in self.neighbors:
            self.neighbors[sender_ip_real]['loss_rate'] = packet_loss 
            print(f"[{self.node_id}] [QoS] Perda de {sender_ip_real}: {packet_loss:.1f}%")

        if self.node_id == target_stream:
            return "SOURCE"
        
        if target_stream in self.routing_table:
            entry = self.routing_table[target_stream]
            return entry.proximo_salto_ip
        return None