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
    HELLO_RESPONSE = 6   # <--- NOVO: Para medir RTT (Pong)
    STREAM_LEAVE = 7     # <--- NOVO: Para parar o stream
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
        
        # Armazena timestamps dos Hellos enviados para calcular RTT
        # { sequence_number: timestamp_envio }
        self.pending_pings: Dict[int, float] = {}

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
        """Recebi um Ping. Devo responder com Pong."""
        now = time.time()
        if sender_ip not in self.neighbors:
            print(f"[{self.node_id}] Novo vizinho detetado: {sender_ip}")
            self.neighbors[sender_ip] = {'metric': 50.0} # Começa com valor conservador
        self.neighbors[sender_ip]['last_seen'] = now
        
        # Preparar payload de resposta com o SEQ original para o remetente calcular RTT
        response_payload = json.dumps({"ack_seq": header['seq']}).encode('utf-8')
        return response_payload

    def handle_hello_response(self, payload, sender_ip):
        """Recebi um Pong. Calcular RTT."""
        try:
            data = json.loads(payload.decode('utf-8'))
            ack_seq = data['ack_seq']
            
            if ack_seq in self.pending_pings:
                start_time = self.pending_pings.pop(ack_seq)
                rtt_ms = (time.time() - start_time) * 1000.0 # Converter para ms
                
                # Suavização da métrica (Média Móvel Exponencial) para evitar oscilações bruscas
                # New_Metric = 0.7 * Old + 0.3 * Current
                old_metric = self.neighbors.get(sender_ip, {}).get('metric', rtt_ms)
                new_metric = (0.7 * old_metric) + (0.3 * rtt_ms)
                
                self.neighbors[sender_ip]['metric'] = new_metric
                # print(f"[METRICAS] RTT para {sender_ip}: {rtt_ms:.2f}ms (Média: {new_metric:.2f})")
        except: pass

    def handle_flood(self, header, payload, sender_ip_real):
        try:
            data = json.loads(payload.decode('utf-8'))
            stream_id = data['stream_id']
            custo_recebido = data['cost']
            origin_seq = data['origin_seq']
        except: return None

        lsa_key = (stream_id, origin_seq)
        if lsa_key in self.lsa_database: return None
        self.lsa_database[lsa_key] = time.time()

        # --- AQUI ESTÁ A MUDANÇA: Usar a métrica real do link ---
        metric_link = self.neighbors.get(sender_ip_real, {}).get('metric', 50.0) # Default se desconhecido
        novo_custo = custo_recebido + metric_link
        # --------------------------------------------------------

        melhorou = False
        if stream_id not in self.routing_table:
            self.routing_table[stream_id] = RouteEntry(stream_id, sender_ip_real, novo_custo)
            melhorou = True
            print(f"[{self.node_id}] 🗺️ Rota: {stream_id} via {sender_ip_real} (Custo {novo_custo:.1f}ms)")
        else:
            rota = self.routing_table[stream_id]
            if novo_custo < rota.custo_acumulado:
                rota.proximo_salto_ip = sender_ip_real
                rota.custo_acumulado = novo_custo
                melhorou = True
                print(f"[{self.node_id}] ♻️ Melhor Rota: {stream_id} via {sender_ip_real} (Custo {novo_custo:.1f}ms)")

        if melhorou:
            data['cost'] = novo_custo
            return json.dumps(data).encode('utf-8')
        return None

    def handle_join(self, payload, sender_ip_real):
        try:
            data = json.loads(payload.decode('utf-8'))
            target_stream = data['stream_id']
        except: return None

        if self.node_id == target_stream:
            if target_stream not in self.routing_table:
                self.routing_table[target_stream] = RouteEntry(target_stream, "SELF", 0.0)
            entry = self.routing_table[target_stream]
            if sender_ip_real not in entry.downstream_ips:
                entry.downstream_ips.add(sender_ip_real)
                print(f"[{self.node_id}] 🎬 CLIENTE REGISTADO! A enviar stream para {sender_ip_real}")
            return "SOURCE"

        if target_stream in self.routing_table:
            entry = self.routing_table[target_stream]
            if sender_ip_real not in entry.downstream_ips:
                entry.downstream_ips.add(sender_ip_real)
                print(f"[{self.node_id}] 🔌 Cliente {sender_ip_real} adicionado ao stream {target_stream}")
                return entry.proximo_salto_ip
        else:
            print(f"[{self.node_id}] ❌ Recebi JOIN para {target_stream} mas não tenho rota!")
            return None

    def handle_leave(self, payload, sender_ip_real):
        """
        Remove um cliente da lista de distribuição.
        Retorna o IP do upstream se for necessário fazer Pruning (poda), ou None.
        """
        try:
            data = json.loads(payload.decode('utf-8'))
            target_stream = data['stream_id']
        except: return None

        if target_stream in self.routing_table:
            entry = self.routing_table[target_stream]
            
            # Remover o cliente da lista de downstream
            if sender_ip_real in entry.downstream_ips:
                entry.downstream_ips.remove(sender_ip_real)
                print(f"[{self.node_id}] ✂️ Cliente {sender_ip_real} saiu do stream {target_stream}")
            
            # Lógica de PRUNING (Poda da Árvore)
            # Se eu não sou a fonte e já não tenho mais clientes, devo avisar o meu upstream
            if len(entry.downstream_ips) == 0 and self.node_id != target_stream:
                print(f"[{self.node_id}] 🍂 Sem mais clientes. Vou pedir LEAVE ao upstream.")
                return entry.proximo_salto_ip
        
        return None