# estado_global.py

import threading
import sys
import time

class OverlayNode:
    """Armazena o estado e dados globais do nó overlay, incluindo locks para Thread Safety."""
    
    def __init__(self, node_id, udp_port):
        self.id = node_id
        self.udp_port = udp_port
        self.running = True # Flag para controlar o ciclo de vida das threads
        
        # --- 1. Estado de Vizinhos (MÉTRICA PING/PONG) ---
        # Tabela usada para medir latência/perda.
        # Estrutura: { 'O2': {'addr': ('ip', port), 'custo_link': inf, 'latencia': 0.0, 'pongs_recebidos': 0, ...} }
        self.tabela_vizinhos = {} 
        self.lock_vizinhos = threading.Lock() 

        # --- 2. Base de Dados LSA (TOPOLOGIA GLOBAL) ---
        # Tabela de adjacência global, usada pelo A*/Dijkstra.
        # Estrutura: { 'O7': {'O2': 1.5, 'C2': 2.0}, 'O2': {'O7': 1.5, 'O1': 1.0}, ... }
        self.tabela_adjacencia = {} 
        self.lock_adjacencia = threading.Lock()
        self.lsa_seq_num = 0

        # --- 3. Fluxos de Multimédia (JOIN/STREAM) ---
        # Usada para armazenar nós 'downstream' para reencaminhamento de STREAM.
        # Estrutura: { 'STREAMER1': {'downstream': ['O7', 'O8']}, ... }
        self.fluxos = {} 
        self.lock_fluxos = threading.Lock()

        # --- 4. Tabela de Rotas/Cache (NEXT HOP) ---
        # Tabela de rotas calculadas pelo A* (CACHE).
        # Estrutura: { 'STREAMER1': { 'proximo_salto': 'O7', 'custo_total': 5.0 }, ... }
        self.tabela_rotas_cache = {}
        self.lock_rotas_cache = threading.Lock()
