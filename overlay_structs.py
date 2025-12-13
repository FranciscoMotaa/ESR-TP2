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
    STREAM_ACK = 11      # ACK de pacotes de stream (cumulative)
    STREAM_NACK = 12     # NACK para retransmissão seletiva
    STREAM_RETX = 13     # Pacote retransmitido
    DEBUG = 99

@dataclass
class RouteEntry:
    source_id: str          
    proximo_salto_ip: str   
    custo_acumulado: float  
    downstream_ips: Set[str] = field(default_factory=set)
    last_update: float = 0.0

@dataclass
class ClientQoSMetrics:
    """Métricas de QoS por cliente"""
    client_ip: str
    last_ack_seq: int = 0           # Último seq recebido com sucesso
    expected_seq: int = 1            # Próximo seq esperado
    packets_received: int = 0
    packets_lost: int = 0
    consecutive_losses: int = 0      # Perdas consecutivas
    loss_rate: float = 0.0           # Taxa de perda (%)
    rtt: float = 0.0                 # RTT estimado (ms)
    last_report_time: float = 0.0
    current_quality: str = 'HIGH'    # HIGH ou LOW
    
    def update_loss_rate(self):
        total = self.packets_received + self.packets_lost
        if total > 0:
            self.loss_rate = (self.packets_lost / total) * 100.0

@dataclass 
class RetransmissionBuffer:
    """Buffer circular para retransmissão"""
    max_size: int = 300              # ~15 segundos a 20fps (era 200)
    buffer: Dict[int, bytes] = field(default_factory=dict)
    
    def add(self, seq: int, data: bytes):
        self.buffer[seq] = data
        # Limpar pacotes antigos
        if len(self.buffer) > self.max_size:
            min_seq = min(self.buffer.keys())
            del self.buffer[min_seq]
    
    def get(self, seq: int) -> Optional[bytes]:
        return self.buffer.get(seq)

@dataclass
class FECBlock:
    """Bloco FEC (k pacotes de dados + 1 paridade XOR)"""
    block_id: int
    k: int = 1                       # 1 pacote = 100% overhead, máxima robustez
    data_packets: Dict[int, bytes] = field(default_factory=dict)
    parity_packet: Optional[bytes] = None
    packet_sizes: Dict[int, int] = field(default_factory=dict)  # Tamanhos originais
    received_count: int = 0
    
    def add_data(self, seq: int, data: bytes):
        """Adiciona pacote de dados ao bloco"""
        if seq not in self.data_packets:  # Evitar contar duplicados
            self.received_count += 1
        self.data_packets[seq] = data
        self.packet_sizes[seq] = len(data)  # Salvar tamanho original
    
    def add_parity(self, parity: bytes):
        """Adiciona pacote de paridade"""
        if self.parity_packet is None:  # Evitar contar duplicados
            self.received_count += 1
        self.parity_packet = parity
    
    def can_recover(self) -> bool:
        """Verifica se pode recuperar pacotes perdidos"""
        # Precisamos: paridade + (k-1) dados = k pacotes totais
        # Isso significa que perdemos exatamente 1 pacote de dados
        has_parity = self.parity_packet is not None
        num_data = len(self.data_packets)
        # Pode recuperar se: tem paridade E tem exatamente k-1 dados (falta 1)
        return has_parity and num_data == (self.k - 1)
    
    def get_missing_seq(self) -> Optional[int]:
        """Retorna sequência do pacote perdido (se houver apenas 1)"""
        expected_seqs = set(range(self.block_id * self.k, (self.block_id + 1) * self.k))
        received_seqs = set(self.data_packets.keys())
        missing = expected_seqs - received_seqs
        return list(missing)[0] if len(missing) == 1 else None
    
    def recover_packet(self) -> Optional[tuple]:
        """Recupera pacote perdido usando XOR"""
        if not self.can_recover() or not self.parity_packet:
            return None
        
        missing_seq = self.get_missing_seq()
        if missing_seq is None:
            return None
        
        # CASO ESPECIAL k=1: Paridade É o próprio dado (cópia)!
        if self.k == 1 and len(self.data_packets) == 0:
            # Dado perdeu, mas temos FEC (que É o dado)
            result = self.parity_packet
            
            # Usar tamanho se disponível
            if missing_seq in self.packet_sizes:
                expected_size = self.packet_sizes[missing_seq]
                result = result[:expected_size]
            
            if len(result) > 0:
                return (missing_seq, bytes(result))
            return None
        
        # CASO GERAL (k>1): XOR de todos os pacotes recebidos com a paridade
        result = bytearray(self.parity_packet)
        for data in self.data_packets.values():
            # Garantir mesmo tamanho (pad com zeros)
            max_len = max(len(result), len(data))
            result.extend(b'\x00' * (max_len - len(result)))
            data_padded = data + b'\x00' * (max_len - len(data))
            
            for i in range(max_len):
                result[i] ^= data_padded[i]
        
        # Usar tamanho exato se disponível, senão calcular
        if missing_seq in self.packet_sizes:
            # Tamanho exato do pacote original
            expected_size = self.packet_sizes[missing_seq]
            result = result[:expected_size]
        elif self.packet_sizes:
            # Média dos tamanhos conhecidos (mais conservador)
            avg_size = sum(self.packet_sizes.values()) // len(self.packet_sizes)
            result = result[:avg_size]
        else:
            # Fallback: usar tamanho razoável baseado em CHUNK_SIZE típico (500 bytes)
            # Limitar a 600 bytes para segurança
            result = result[:600]
        
        # Validação final: dados não vazios
        if len(result) == 0:
            return None
        
        return (missing_seq, bytes(result))

class FECEncoder:
    """Codificador FEC usando XOR"""
    
    @staticmethod
    def generate_parity(packets: List[bytes]) -> bytes:
        """Gera pacote de paridade (XOR de todos os pacotes)"""
        if not packets:
            return b''
        
        # Encontrar tamanho máximo
        max_len = max(len(p) for p in packets)
        
        # XOR de todos os pacotes
        parity = bytearray(max_len)
        for packet in packets:
            packet_padded = packet + b'\x00' * (max_len - len(packet))
            for i in range(max_len):
                parity[i] ^= packet_padded[i]
        
        return bytes(parity)

class FECDecoder:
    """Decodificador FEC no receptor"""
    
    def __init__(self, k: int = 1):  # k=1 (100% overhead) máxima robustez
        self.k = k  # Pacotes por bloco
        self.blocks: Dict[int, FECBlock] = {}
        self.recovered_packets: Dict[int, bytes] = {}  # Pacotes recuperados
        # Estatísticas para diagnóstico
        self.stats_blocks_complete = 0  # Blocos com todos pacotes
        self.stats_blocks_recovered = 0  # Blocos recuperados via FEC
        self.stats_blocks_failed = 0  # Blocos irrecuperáveis
    
    def get_block_id(self, seq: int) -> int:
        """Calcula ID do bloco FEC para uma sequência"""
        return seq // self.k
    
    def add_data_packet(self, seq: int, data: bytes):
        """Adiciona pacote de dados"""
        block_id = self.get_block_id(seq)
        if block_id not in self.blocks:
            self.blocks[block_id] = FECBlock(block_id=block_id, k=self.k)
        
        self.blocks[block_id].add_data(seq, data)
        self._try_recover(block_id)
    
    def add_fec_packet(self, block_id: int, parity: bytes, sizes: Optional[List[int]] = None):
        """Adiciona pacote FEC (paridade) com tamanhos opcionais"""
        if block_id not in self.blocks:
            self.blocks[block_id] = FECBlock(block_id=block_id, k=self.k)
        
        # Evitar processar FEC duplicado
        if self.blocks[block_id].parity_packet is not None:
            return  # Já temos paridade para este bloco
        
        self.blocks[block_id].add_parity(parity)
        
        # Armazenar tamanhos se fornecidos
        if sizes:
            first_seq = block_id * self.k
            for i, size in enumerate(sizes):
                seq = first_seq + i
                self.blocks[block_id].packet_sizes[seq] = size
        
        self._try_recover(block_id)
    
    def _try_recover(self, block_id: int):
        """Tenta recuperar pacotes perdidos"""
        block = self.blocks[block_id]
        num_data = len(block.data_packets)
        has_parity = block.parity_packet is not None
        
        if block.can_recover():
            result = block.recover_packet()
            if result:
                seq, data = result
                self.recovered_packets[seq] = data
                self.stats_blocks_recovered += 1
        else:
            # Bloco não recuperável
            if num_data == self.k and has_parity:
                self.stats_blocks_complete += 1  # Bloco completo (sem perdas)
            elif num_data < self.k - 1 or not has_parity:
                self.stats_blocks_failed += 1  # Bloco irrecuperável
        
        # Debug periódico
        if block_id % 100 == 0 and block_id > 0:
            total = self.stats_blocks_complete + self.stats_blocks_recovered + self.stats_blocks_failed
            if total > 0:
                rec_rate = (self.stats_blocks_recovered / total * 100)
                print(f"\n[FEC-DIAGNOSTIC] Blocos: Completos={self.stats_blocks_complete} Recuperados={self.stats_blocks_recovered} Falhos={self.stats_blocks_failed} (Taxa Rec: {rec_rate:.1f}%)")
    
    def get_recovered(self, seq: int) -> Optional[bytes]:
        """Obtém pacote recuperado"""
        return self.recovered_packets.pop(seq, None)
    
    def cleanup_old_blocks(self, current_seq: int):
        """Remove blocos antigos para economizar memória"""
        current_block = self.get_block_id(current_seq)
        # Aumentar janela de 5 para 10 blocos (60 pacotes) para suportar retransmissões
        old_blocks = [bid for bid in self.blocks.keys() if bid < current_block - 10]
        for bid in old_blocks:
            del self.blocks[bid]
        # Aumentar janela de pacotes recuperados de 40 para 80
        old_recovered = [seq for seq in self.recovered_packets.keys() if seq < current_seq - 80]
        for seq in old_recovered:
            del self.recovered_packets[seq]

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
        self.fec_decoder = FECDecoder(k=1)  # k=1 (100% overhead) máxima robustez

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
        try:
            data = json.loads(payload.decode('utf-8'))
            ack_seq = data['ack_seq']
            if ack_seq in self.pending_pings:
                start_time = self.pending_pings.pop(ack_seq)
                rtt_ms = (time.time() - start_time) * 1000.0 
                old_metric = self.neighbors.get(sender_ip, {}).get('metric', rtt_ms)
                new_metric = (0.7 * old_metric) + (0.3 * rtt_ms)
                self.neighbors[sender_ip]['metric'] = new_metric
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
        if sender_ip_real not in self.neighbors: return None

        # Verificar duplicados (Loop prevention)
        lsa_key = (stream_id, origin_seq)
        if lsa_key in self.lsa_database: return None
        self.lsa_database[lsa_key] = time.time()

        metric_link = self.neighbors[sender_ip_real]['metric']
        novo_custo = custo_recebido + metric_link

        # --- AQUI COMEÇA A MUDANÇA CRÍTICA ---
        should_propagate = False
        CHANGE_THRESHOLD = 0.15 # 15% de Histerese para evitar oscilação

        if stream_id not in self.routing_table:
            # Rota nova: Aceitar sempre
            self.routing_table[stream_id] = RouteEntry(stream_id, sender_ip_real, novo_custo)
            should_propagate = True
        else:
            rota = self.routing_table[stream_id]
            
            # CASO 1: Encontrámos um caminho MELHOR (Lower bound)
            # Só trocamos se for realmente melhor para evitar "flapping" por 1ms
            if novo_custo < rota.custo_acumulado:
                rota.proximo_salto_ip = sender_ip_real
                rota.custo_acumulado = novo_custo
                should_propagate = True
            
            # CASO 2: O caminho ATUAL piorou (Upper bound / Congestionamento)
            # Se o meu fornecedor atual diz que o custo subiu, eu TENHO de aceitar a má notícia
            # Mas aplicamos o Threshold para não propagar ruído pequeno
            elif sender_ip_real == rota.proximo_salto_ip:
                if novo_custo > rota.custo_acumulado * (1 + CHANGE_THRESHOLD):
                     rota.custo_acumulado = novo_custo
                     should_propagate = True

        if should_propagate:
            data['cost'] = novo_custo
            # Pequeno Jitter para evitar "Broadcast Storms" síncronas
            time.sleep(0.005) 
            return json.dumps(data).encode('utf-8')
            
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
            entry = self.routing_table[target_stream]
            if sender_ip_real not in entry.downstream_ips:
                entry.downstream_ips.add(sender_ip_real)
                print(f"[{self.node_id}] >> NOVO CLIENTE: {sender_ip_real}")
            return "SOURCE", True # True = Enviar ACK

        # Se sou Router
        if target_stream in self.routing_table:
            entry = self.routing_table[target_stream]
            if sender_ip_real not in entry.downstream_ips:
                entry.downstream_ips.add(sender_ip_real)
                print(f"[{self.node_id}] >> Cliente adicionado: {sender_ip_real}")
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
                print(f"[{self.node_id}] >> Cliente saiu: {sender_ip_real}")
            
            if len(entry.downstream_ips) == 0 and self.node_id != target_stream:
                print(f"[{self.node_id}] >> Sem clientes. Pedindo corte ao upstream.")
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
            print(f"[{self.node_id}] >> Relatorio {client_id}: Perda {packet_loss:.1f}%")
            return "SOURCE"
        
        if target_stream in self.routing_table:
            entry = self.routing_table[target_stream]
            return entry.proximo_salto_ip
        return None