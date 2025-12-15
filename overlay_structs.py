import struct
import json
import time
import os
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Dict, Set, Optional, Tuple
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# --- CONSTANTES ---
MAX_PACKET_SIZE = 4096 
HEADER_FORMAT = "!B 16s 16s I d B" # Type, SrcIP, DstIP, Seq, Timestamp, Encrypted
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
IV_SIZE = 12  # GCM nonce size
TAG_SIZE = 16  # GCM authentication tag size

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

# --- SISTEMA DE CRIPTOGRAFIA ---
class SecurityManager:
    """
    Gestor de Segurança: AES-256-GCM
    
    - AES-256: Cifra simétrica (256-bit key)
    - GCM: Galois/Counter Mode (autenticação + confidencialidade)
    - Overhead: 28 bytes (12B IV + 16B tag)
    - Performance: ~500 MB/s com AES-NI
    """
    
    def __init__(self):
        self.enabled = False
        self.aesgcm = None
        self.key = None
    
    def enable(self, passphrase: str, salt: bytes = b'overlay_stream_2025'):
        """
        Ativa criptografia com derivação de chave PBKDF2.
        
        Args:
            passphrase: Senha/chave mestra
            salt: Salt para PBKDF2 (deve ser único por aplicação)
        """
        # Derivar chave de 256-bit da passphrase
        # Reduzido para 10k iterações (trade-off: startup rápido vs força brute-force)
        # 10k ainda é seguro (NIST mínimo: 1k, recomendado: 10k+)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,  # 256 bits
            salt=salt,
            iterations=10000  # Otimizado para baixa latência
        )
        self.key = kdf.derive(passphrase.encode('utf-8'))
        self.aesgcm = AESGCM(self.key)
        self.enabled = True
        print(f"[SECURITY] Criptografia AES-256-GCM ativada ✓")
    
    def is_enabled(self) -> bool:
        """Verifica se criptografia está ativa"""
        return self.enabled
    
    def encrypt(self, plaintext: bytes) -> Tuple[bytes, bytes, bytes]:
        """
        Cifra dados com AES-256-GCM.
        
        Args:
            plaintext: Dados em claro
        
        Returns:
            (iv, ciphertext, tag): IV (12B), texto cifrado, tag autenticação (16B)
        """
        if not self.enabled:
            raise RuntimeError("Criptografia não está ativada")
        
        # Gerar nonce aleatório (DEVE ser único por mensagem)
        iv = os.urandom(12)
        
        # AES-GCM: cifra + autentica em uma operação
        # Retorna: ciphertext || tag (últimos 16 bytes)
        ciphertext_with_tag = self.aesgcm.encrypt(iv, plaintext, None)
        
        # Separar ciphertext e tag
        ciphertext = ciphertext_with_tag[:-16]
        tag = ciphertext_with_tag[-16:]
        
        return iv, ciphertext, tag
    
    def decrypt(self, iv: bytes, ciphertext: bytes, tag: bytes) -> Optional[bytes]:
        """
        Decifra dados com AES-256-GCM.
        
        Args:
            iv: Nonce (12 bytes)
            ciphertext: Dados cifrados
            tag: Tag de autenticação (16 bytes)
        
        Returns:
            Dados em claro ou None se falhar autenticação
        """
        if not self.enabled:
            raise RuntimeError("Criptografia não está ativada")
        
        try:
            # Reconstruir formato esperado: ciphertext || tag
            ciphertext_with_tag = ciphertext + tag
            
            # Decifrar e verificar autenticação
            plaintext = self.aesgcm.decrypt(iv, ciphertext_with_tag, None)
            return plaintext
        
        except Exception as e:
            # Falha na autenticação ou dados corrompidos
            print(f"[SECURITY] Falha ao decifrar: {e}")
            return None

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
        self.fec_decoder = FECDecoder(k=1)  # k=1 (100% overhead) máxima robustez
        self.security = SecurityManager()  # Gestor de segurança
        
        # Estatísticas de segurança
        self.stats_encrypted_sent = 0
        self.stats_encrypted_recv = 0
        self.stats_decrypt_failed = 0
    
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
        updated_routes = []

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
                            # registrar rota atualizada para notificar a camada de rede (main) a enviar JOIN upstream
                            updated_routes.append((stream_id, best_sender))
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

                return dead_neighbors, updated_routes

    def pack_message(self, msg_type: MsgType, dest_ip: str, payload: bytes = b"", encrypt: bool = True) -> bytes:
        """
        Cria pacote com header + payload (cifrado por padrão).
        
        Args:
            msg_type: Tipo de mensagem
            dest_ip: IP destino
            payload: Dados
            encrypt: Se True, cifra payload com AES-GCM (padrão: True)
        
        Returns:
            Pacote completo (header sempre claro, payload cifrado por padrão)
        """
        self.sequence_number += 1
        timestamp = time.time()
        src_ip_bytes = self.ip.encode('utf-8').ljust(16, b'\0')
        dest_ip_bytes = dest_ip.encode('utf-8').ljust(16, b'\0')
        
        # Cifrar payload se solicitado E segurança ativa
        is_encrypted = 0  # Flag: 0 = claro, 1 = cifrado
        if encrypt and self.security.is_enabled():
            iv, ciphertext, tag = self.security.encrypt(payload)
            payload = iv + ciphertext + tag
            is_encrypted = 1
            self.stats_encrypted_sent += 1
        
        # Header SEMPRE em claro (para roteamento)
        header = struct.pack(HEADER_FORMAT, msg_type.value, src_ip_bytes, dest_ip_bytes, 
                           self.sequence_number, timestamp, is_encrypted)
        return header + payload

    def unpack_message(self, data: bytes):
        """
        Extrai header + payload (decifrando se necessário).
        
        Returns:
            (header_dict, payload) ou (None, None) se inválido
        """
        if len(data) < HEADER_SIZE: return None, None
        header_bytes = data[:HEADER_SIZE]
        payload = data[HEADER_SIZE:]
        
        # Desempacotar header (agora com flag de encriptação)
        msg_type_val, src_ip_raw, dst_ip_raw, seq, ts, is_encrypted = struct.unpack(HEADER_FORMAT, header_bytes)
        
        # Decifrar payload se necessário
        if is_encrypted == 1 and self.security.is_enabled():
            # Extrair IV, ciphertext e tag
            if len(payload) < (IV_SIZE + TAG_SIZE):
                print(f"[SECURITY] Pacote cifrado muito pequeno (seq={seq})")
                self.stats_decrypt_failed += 1
                return None, None
            
            iv = payload[:IV_SIZE]
            ciphertext = payload[IV_SIZE:-TAG_SIZE]
            tag = payload[-TAG_SIZE:]
            
            # Decifrar
            plaintext = self.security.decrypt(iv, ciphertext, tag)
            if plaintext is None:
                src_ip_clean = src_ip_raw.decode('utf-8').strip('\x00')
                print(f"[SECURITY] Falha autenticação pacote seq={seq} de {src_ip_clean}")
                self.stats_decrypt_failed += 1
                return None, None
            
            payload = plaintext
            self.stats_encrypted_recv += 1
        
        elif is_encrypted == 1 and not self.security.is_enabled():
            # Pacote cifrado mas segurança não ativa
            print(f"[SECURITY] Pacote cifrado recebido mas criptografia não está ativa!")
            return None, None
        
        try:
            msg_type_enum = MsgType(msg_type_val)
        except ValueError:
            # Mensagem com tipo desconhecido -> ignorar para robustez
            try:
                src_ip_clean = src_ip_raw.decode('utf-8').strip('\x00')
            except Exception:
                src_ip_clean = '<unknown>'
            print(f"[WARN] Mensagem com MsgType desconhecido: {msg_type_val} de {src_ip_clean}")
            return None, None

        return {
            "type": msg_type_enum,
            "source_ip": src_ip_raw.decode('utf-8').strip('\0'),
            "dest_ip": dst_ip_raw.decode('utf-8').strip('\0'),
            "seq": seq,
            "timestamp": ts,
            "encrypted": is_encrypted == 1
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
            print(f"[{self.node_id}]    (flood recebido de {sender_ip_real}, origin_seq={origin_seq})")
            time.sleep(0.002)  # Pequeno jitter
            return json.dumps(data).encode('utf-8')
        else:
            print(f"[{self.node_id}] 🚫 NÃO propagando {stream_id} (flood de {sender_ip_real} custo {novo_custo:.2f} não melhora {self.routing_table.get(stream_id, RouteEntry('', '', float('inf'))).custo_acumulado:.2f})")
            
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
        except:
            print(f"[{self.node_id}] [handle_join] payload inválido de {sender_ip_real}")
            return None, False, False

        print(f"[{self.node_id}] [handle_join] pedido de {sender_ip_real} para stream {target_stream}")

        # Se sou Streamer (SOURCE)
        if self.node_id == target_stream:
            if target_stream not in self.routing_table:
                self.routing_table[target_stream] = RouteEntry(target_stream, "SELF", 0.0)
                print(f"[{self.node_id}] 📝 Entrada de roteamento criada para SELF")
            entry = self.routing_table[target_stream]
            was_first = len(entry.downstream_ips) == 0
            if sender_ip_real not in entry.downstream_ips:
                entry.downstream_ips.add(sender_ip_real)
                print(f"[{self.node_id}] >> NOVO CLIENTE: {sender_ip_real}")
            already_receiving = not was_first
            print(f"[{self.node_id}] [handle_join] retorno: SOURCE, ack=True, already_receiving={already_receiving}")
            return "SOURCE", True, already_receiving # True = Enviar ACK

        # Se sou Router e JÁ tenho rota para este stream
        if target_stream in self.routing_table:
            entry = self.routing_table[target_stream]
            was_first = len(entry.downstream_ips) == 0
            if sender_ip_real not in entry.downstream_ips:
                entry.downstream_ips.add(sender_ip_real)
                print(f"[{self.node_id}] >> Cliente adicionado: {sender_ip_real}")
            already_receiving = not was_first
            print(f"[{self.node_id}] [handle_join] retorno: up={entry.proximo_salto_ip}, ack=True, already_receiving={already_receiving}")
            return entry.proximo_salto_ip, True, already_receiving # True = Enviar ACK
        
        # Não tenho rota para este stream
        return None, False, False

    def handle_leave(self, payload, sender_ip_real):
        try:
            data = json.loads(payload.decode('utf-8'))
            target_stream = data['stream_id']
        except: return None

        print(f"[{self.node_id}] [handle_leave] pedido de {sender_ip_real} para stream {target_stream}")

        if target_stream in self.routing_table:
            entry = self.routing_table[target_stream]
            if sender_ip_real in entry.downstream_ips:
                entry.downstream_ips.remove(sender_ip_real)
                print(f"[{self.node_id}] >> Cliente saiu: {sender_ip_real}")
            
            if len(entry.downstream_ips) == 0 and self.node_id != target_stream:
                print(f"[{self.node_id}] >> Sem clientes. Pedindo corte ao upstream.")
                print(f"[{self.node_id}] [handle_leave] retorno: up={entry.proximo_salto_ip}")
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