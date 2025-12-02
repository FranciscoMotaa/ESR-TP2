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
    DEBUG = 99


@dataclass
class RouteEntry:
    source_id: str
    proximos_salto_ip: str
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
        if len(data) < HEADER_SIZE:
            return None, None
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
            self.neighbors[sender_ip] = {'metric': 50.0, 'lastseen': now}
        else:
            self.neighbors[sender_ip]['lastseen'] = now
        
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
                rtt_ms = (time.time() - start_time) * 1000.0
                
                # Suavização da métrica (Média Móvel Exponencial)
                old_metric = self.neighbors.get(sender_ip, {}).get('metric', rtt_ms)
                new_metric = (0.7 * old_metric) + (0.3 * rtt_ms)
                self.neighbors[sender_ip]['metric'] = new_metric
        except:
            pass


    def handle_flood(self, header, payload, sender_ip_real):
    
        # ========== PASSO 1: Extrair dados do PAYLOAD ==========
        try:
            data = json.loads(payload.decode('utf-8'))
            stream_id = data['stream_id']
            custo_recebido = data['cost']
            origin_seq = data['origin_seq']
            
            print(f"\n[{self.node_id}] FLOOD RECEBIDO")
            print(f"    De: {sender_ip_real}")
            print(f"    Payload: stream_id={stream_id}, cost={custo_recebido:.1f}ms, seq={origin_seq}")

        except Exception as e:
            print(f"[{self.node_id}] Erro ao parse payload FLOOD: {e}")
            return None
        
        
        # ========== PASSO 2: Verificar se já vimos esta mensagem (evitar loops) ==========
        lsa_key = (stream_id, origin_seq)
        
        if lsa_key in self.lsa_database:
            print(f"    LSA duplicado (já vimos seq {origin_seq}). Ignorando.")
            return None
        
        # Marcar como visto
        self.lsa_database[lsa_key] = time.time()
        print(f"    LSA novo (seq {origin_seq}). Processando...")
        
        
        # ========== PASSO 3: Calcular custo via este link ==========
        neighbor_info = self.neighbors.get(sender_ip_real, {})
        metric_rtt = neighbor_info.get('metric', 50.0)
        # Permitir um atraso adicional configurado por vizinho (ms)
        extra_delay = float(neighbor_info.get('delay', 0.0))

        metric_link = metric_rtt + extra_delay

        print(f"\n    Cálculo de Custo:")
        print(f"       Custo recebido (origem → sender): {custo_recebido:.1f}ms")
        print(f"       RTT deste link ({sender_ip_real}): {metric_rtt:.1f}ms")
        print(f"       Atraso extra configurado: {extra_delay:.1f}ms")
        print(f"       Latência total deste link: {metric_link:.1f}ms")

        novo_custo = custo_recebido + metric_link
        print(f"       Novo custo total: {custo_recebido:.1f} + {metric_link:.1f} = {novo_custo:.1f}ms")
        
        
        # ========== PASSO 4: Comparar com rota anterior e decidir ==========
        melhorou = False
        
        if stream_id not in self.routing_table:
            # Primeira rota para este stream
            self.routing_table[stream_id] = RouteEntry(stream_id, sender_ip_real, novo_custo)
            melhorou = True
            print(f"\n    Nova rota adicionada para stream {stream_id} via {sender_ip_real} com custo {novo_custo:.1f}ms")
        else:
            entry = self.routing_table[stream_id]
            
            print(f"\n    Comparação com rota existente:")
            print(f"       Rota anterior: via {entry.proximos_salto_ip} (custo {entry.custo_acumulado:.1f}ms)")
            print(f"       Rota nova:     via {sender_ip_real} (custo {novo_custo:.1f}ms)")
            
            if novo_custo < entry.custo_acumulado:
                # Rota melhor encontrada
                diferenca = entry.custo_acumulado - novo_custo
                entry.proximos_salto_ip = sender_ip_real
                entry.custo_acumulado = novo_custo
                melhorou = True
                
                print(f"       MELHORIA! Economiza {diferenca:.1f}ms")
                print(f"    Rota ATUALIZADA para stream {stream_id} via {sender_ip_real} com custo {novo_custo:.1f}ms")
            else:
                print(f"       Rota existente é melhor/igual. Mantendo.")
        
        
        # ========== PASSO 5: Propagar FLOOD para vizinhos (se melhorou) ==========
        if melhorou:
            print(f"\n    Reencaminhando FLOOD para vizinhos...")
            data['cost'] = novo_custo
            payload_novo = json.dumps(data).encode('utf-8')
            print(f"    Novo payload: cost={novo_custo:.1f}ms, seq={origin_seq}\n")
            return payload_novo
        else:
            print(f"\n    Não propaga (rota não melhorou)\n")
            return None

    def handle_leave(self, payload, sender_ip_real):
        """
        Processa STREAM_LEAVE do cliente.
        Remove cliente e ativa pruning da árvore se necessário.
        """
        try:
            data = json.loads(payload.decode('utf-8'))
            target_stream = data['stream_id']
        except:
            return None

        if target_stream in self.routing_table:
            entry = self.routing_table[target_stream]
            
            # Remover o cliente da lista de downstream
            if sender_ip_real in entry.downstream_ips:
                entry.downstream_ips.remove(sender_ip_real)
                print(f"[{self.node_id}] Cliente {sender_ip_real} saiu do stream {target_stream}")
            
            # Lógica de PRUNING (Poda da Árvore)
            # Se não há mais clientes e não sou a fonte, avisar upstream
            if len(entry.downstream_ips) == 0 and self.node_id != target_stream:
                print(f"[{self.node_id}] Sem mais clientes. Vou pedir LEAVE ao upstream.")
                return entry.proximos_salto_ip
        
        return None

    def handle_join(self, payload, sender_ip_real):
        """
        Processa um STREAM_JOIN recebido de um vizinho/cliente.

        - Se for a fonte do stream, adiciona o sender aos downstreams e NÃO reencaminha.
        - Se tiver uma rota para o stream, adiciona o sender aos downstreams e devolve o next-hop (upstream) para reencaminhar.
        - Se não houver rota, retorna None (o pedido fica sem resposta neste nó).
        """
        try:
            data = json.loads(payload.decode('utf-8'))
            stream_id = data.get('stream_id')
        except Exception:
            return None

        if not stream_id:
            return None

        # Se eu sou a fonte, adiciono o cliente directamente
        if self.node_id == stream_id:
            entry = self.routing_table.get(stream_id)
            if not entry:
                # Criar entrada local como fonte
                self.routing_table[stream_id] = RouteEntry(stream_id, self.ip, 0.0)
                entry = self.routing_table[stream_id]
            entry.downstream_ips.add(sender_ip_real)
            print(f"[{self.node_id}] Cliente {sender_ip_real} ligado directamente à fonte {stream_id}")
            return "SOURCE"

        # Se conheço rota para o stream, inscrever o cliente e pedir upstream
        if stream_id in self.routing_table:
            entry = self.routing_table[stream_id]
            entry.downstream_ips.add(sender_ip_real)
            print(f"[{self.node_id}] Cliente {sender_ip_real} adicionado a downstream de {stream_id}; reencaminhar JOIN para {entry.proximos_salto_ip}")
            return entry.proximos_salto_ip

        # Sem rota: nada a fazer por agora
        print(f"[{self.node_id}] JOIN para {stream_id} recebido mas sem rota conhecida. Ignorando/aguardando rota.")
        return None


# Compatibilidade: exportar nome esperado por `main.py`
MAXPACKETSIZE = MAX_PACKET_SIZE
