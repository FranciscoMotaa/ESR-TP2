# (Dentro da classe OTTNode, ao lado das outras threads)

def thread_verificar_vizinhos(self):
    """
    Trabalhador 3 (Verificador 🕵️‍♂️). 
    Corre para remover vizinhos "mortos".
    """
    print(f"[{self.node_name}] THREAD: Verificador de Vizinhos iniciado.")
    
    # Os teus valores:
    CHECK_INTERVAL = 20 # A tua ideia: verificar a cada 20s
    TIMEOUT_DURATION = 20 # A tua ideia: 20s sem notícias = "morto"
    
    while self.running:
        
        # 1. Espera 20 segundos antes da próxima verificação
        time.sleep(CHECK_INTERVAL) 
        
        print(f"[{self.node_name}] Verificador: A verificar vizinhos mortos...")
        
        current_time = time.time() # Pega na hora atual ⏰
        vizinhos_mortos = []      # Uma lista para guardar quem "morreu"
        
        # 2. "Tranca" 🔐 e LÊ a tabela
        with self.lock_tabela:
            # Faz um loop por todos os vizinhos na tabela
            for nome_vizinho, dados_vizinho in self.tabela_vizinhos.items():
                older = dados_vizinho['last_heartbeat']
                
                if current_time - older >= TIMEOUT_DURATION:
                    vizinhos_mortos.append(nome_vizinho)
                
                if vizinhos_mortos: # Só faz isto se a lista não estiver vazia
                    print(f"[{self.node_name}] ALERTA: Vizinhos mortos detetados: {vizinhos_mortos}")
            
            # "Tranca" 🔐 a tabela principal para a podermos editar
        with self.lock_tabela: 
                # O teu loop!
            for nome in vizinhos_mortos:
                    # A tua ação!
                self.tabela_vizinhos.pop(nome, None) # (O 'None' evita um erro se o nome já tiver sido removido por outra razão)

        # 4. Remove os mortos da tabela (se houver)
        if vizinhos_mortos:
            print(f"[{self.node_name}] ALERTA: Vizinhos mortos detetados: {vizinhos_mortos}")
            
            with self.lock_tabela: # Precisa do "lock" outra vez para escrever
                for nome in vizinhos_mortos:
                    self.tabela_vizinhos.pop(nome, None) # Remove da tabela