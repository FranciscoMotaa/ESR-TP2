import json
import time
import sys
import os
import matplotlib.pyplot as plt
from datetime import datetime

# Configuração
MAX_POINTS = 50 

def main():
    if len(sys.argv) < 2:
        print("Uso: python3 dashboard.py <NODE_ID>")
        print("Ex: python3 dashboard.py R1")
        sys.exit(1)

    node_to_watch = sys.argv[1]
    log_file = f"metrics_{node_to_watch}.json"

    # Configuração do Matplotlib
    try:
        plt.style.use('dark_background')
    except: pass
    
    # Ativar modo interativo
    plt.ion()
    fig, ax = plt.subplots()
    try:
        fig.canvas.manager.set_window_title(f'Monitorização RTT - Nó {node_to_watch}')
    except: pass

    history = {} # { 'ip': ([tempos], [rtts]) }

    print(f"[*] A iniciar Dashboard para {node_to_watch}...")
    print("Pressione Ctrl+C para sair.")

    try:
        while True:
            # 1. Ler Dados
            if os.path.exists(log_file):
                try:
                    with open(log_file, 'r') as f:
                        data = json.load(f)
                    
                    current_time = datetime.now()
                    active_neighbors = []
                    
                    if 'neighbors' in data:
                        for neighbor in data['neighbors']:
                            ip = neighbor['ip']
                            rtt = neighbor['rtt']
                            active_neighbors.append(ip)
                            
                            if ip not in history:
                                history[ip] = ([], [])
                            
                            times, rtts = history[ip]
                            times.append(current_time)
                            rtts.append(rtt)
                            
                            if len(times) > MAX_POINTS:
                                history[ip] = (times[-MAX_POINTS:], rtts[-MAX_POINTS:])
                except: pass

            # 2. Desenhar
            ax.clear()
            if not history:
                ax.text(0.5, 0.5, "A aguardar dados...", ha='center', va='center')
            else:
                for ip, (times, rtts) in history.items():
                    # Opcional: mostrar apenas vizinhos ativos recentemente
                    ax.plot(times, rtts, label=f"{ip} ({rtts[-1]:.1f}ms)", marker='o', markersize=3)
                
                ax.legend(loc='upper right')

            ax.set_title(f"Latência em Tempo Real (RTT) - {node_to_watch}")
            ax.set_ylabel("RTT (ms)")
            ax.set_xlabel("Tempo")
            ax.grid(True, linestyle='--', alpha=0.3)
            plt.xticks(rotation=45)
            plt.tight_layout()

            # 3. Atualizar a Janela
            plt.draw()
            plt.pause(1.0) # Espera 1 segundo e processa eventos GUI

    except KeyboardInterrupt:
        print("\nA sair...")
        plt.close()

if __name__ == "__main__":
    main()