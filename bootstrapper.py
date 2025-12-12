import socket
import threading
import json
import sys
import time
from datetime import datetime
from collections import defaultdict

# Configuração do Tracker
BIND_IP = "0.0.0.0"
BIND_PORT = 6000
MONITOR_PORT = 6001  # Porta UDP para receber updates de estado
NODE_DEFAULT_PORT = 50000  # Porta que os nós usam para comunicação (e receber notificações)

# Estruturas de dados para monitorização
# node_state: {node_id: {'ip': ..., 'last_seen': ..., 'neighbors': {...}, 'routing_table': {...}, 'streams': [...]}}
node_state = {}
route_history = {}
neighbor_discovery = {}
state_lock = threading.Lock()


def load_topology():
	"""Inicializa o Tracker em modo dinâmico."""
	# Nenhuma topologia estática é carregada.
	print("[*] Tracker inicializado. Descoberta de vizinhos dinâmica ativa.")


def notify_new_node(new_node_id, new_node_ip):
	"""Notifica todos os nós ativos sobre a entrada de um novo nó."""

	notification = json.dumps({
		"type": "neighbor_update",
		"new_neighbor": new_node_ip,
		"neighbor_id": new_node_id
	}).encode("utf-8")

	print(f"[*] A notificar todos os nós ativos sobre o novo nó {new_node_id} ({new_node_ip}).")

	with state_lock:
		# Iterar sobre todos os nós já registados (exceto o próprio)
		for other_node_id, info in node_state.items():
			if other_node_id == new_node_id:
				continue
			try:
				sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
				sock.settimeout(0.5)

				other_ip = info.get("ip")
				if not other_ip:
					sock.close()
					continue

				# Enviar notificação para a porta padrão de comunicação do nó (50000)
				sock.sendto(notification, (other_ip, NODE_DEFAULT_PORT))
				sock.close()

				print(f"   -> Notificado {other_node_id} ({other_ip}) sobre novo nó {new_node_id}")
			except Exception:
				# Falha ao notificar um nó (pode estar offline ou inacessível)
				pass


def handle_client(client_sock, addr):
	"""Lida com pedidos de registo TCP (Bootstrapping Dinâmico)."""
	try:
		data = client_sock.recv(4096)
		if not data:
			return

		try:
			request = json.loads(data.decode("utf-8"))
		except Exception:
			print(f"[TRACKER] Pedido inválido de {addr}: {data}")
			return

		node_id = request.get("id")
		node_ip = request.get("ip")

		print(f"[TRACKER] Pedido de registo dinâmico de {node_id} ({node_ip})")

		response_neighbors = []
		is_new_registration = False

		with state_lock:
			# 1. Obter a lista de *todos* os IPs de nós já ativos (exceto o próprio)
			all_active_ips = [info.get("ip") for n_id, info in node_state.items() if n_id != node_id]
			response_neighbors = all_active_ips

			# 2. Registar/Atualizar nó no estado
			if node_id not in node_state:
				node_state[node_id] = {
					"ip": node_ip,
					"last_seen": time.time(),
					"neighbors": {},
					"routing_table": {},
					"streams": []
				}
				is_new_registration = True
				print(f"   -> Novo nó {node_id} registado. Devolvendo {len(all_active_ips)} vizinhos iniciais.")
			else:
				# Atualizar IP e last_seen em caso de reconexão
				node_state[node_id]["ip"] = node_ip
				node_state[node_id]["last_seen"] = time.time()

		# 3. Enviar a lista de vizinhos (IPs dos nós ativos) ao novo nó
		response = json.dumps({"status": "OK", "neighbors": response_neighbors})
		try:
			client_sock.send(response.encode("utf-8"))
			print(f"[TRACKER] Respondido a {node_id} ({node_ip}) -> {response}")
		except Exception as e:
			print(f"[TRACKER] Falha a enviar resposta a {node_id}: {e}")

		# 4. Notificar a rede sobre o novo nó (após enviar resposta)
		if is_new_registration:
			threading.Thread(target=notify_new_node, args=(node_id, node_ip), daemon=True).start()

	except Exception as e:
		print(f"[ERRO] {e}")
	finally:
		try:
			client_sock.close()
		except Exception:
			pass


def handle_state_update(sock):
	"""Recebe updates de estado dos nós via UDP."""
	while True:
		try:
			data, addr = sock.recvfrom(8192)
			try:
				update = json.loads(data.decode("utf-8"))
			except Exception:
				continue

			node_id = update.get("node_id")
			if not node_id:
				continue

			with state_lock:
				# Inicializar estruturas para um nó recém-visto (se o TCP falhou ou foi ignorado)
				if node_id not in node_state:
					node_state[node_id] = {
						"ip": addr[0],
						"last_seen": time.time(),
						"neighbors": {},
						"routing_table": {},
						"streams": []
					}
					neighbor_discovery[node_id] = {}
					route_history[node_id] = {}

				current_time = time.time()

				# Detectar novos vizinhos (descoberta dinâmica)
				new_neighbors = update.get("neighbors", {})
				if node_id in neighbor_discovery:
					for neighbor_ip in new_neighbors:
						if neighbor_ip not in neighbor_discovery[node_id]:
							neighbor_discovery[node_id][neighbor_ip] = current_time

				# Detectar mudanças de rotas e logar (código inalterado)
				new_routing_table = update.get("routing_table", {})
				old_routing_table = node_state[node_id].get("routing_table", {})

				for dest_id, new_route in new_routing_table.items():
					new_next_hop = new_route.get("next_hop")
					new_cost = new_route.get("cost", 0)

					if node_id not in route_history:
						route_history[node_id] = {}
					if dest_id not in route_history[node_id]:
						route_history[node_id][dest_id] = []

					if dest_id in old_routing_table:
						old_next_hop = old_routing_table[dest_id].get("next_hop")
						old_cost = old_routing_table[dest_id].get("cost", 0)

						# Logar mudanças de next_hop
						if new_next_hop != old_next_hop:
							route_history[node_id][dest_id].append((current_time, new_next_hop, new_cost))
							cost_diff = new_cost - old_cost
							if abs(cost_diff) > 10.0:
								indicator = "↓" if cost_diff < 0 else "↑"
								print(f"[{node_id}] ROTA p/ {dest_id}: {old_next_hop}({old_cost:.0f}ms) → {new_next_hop}({new_cost:.0f}ms) [{indicator}{abs(cost_diff):.0f}ms]")
						# Logar grandes mudanças de custo na mesma rota
						elif abs(new_cost - old_cost) > 15.0:
							route_history[node_id][dest_id].append((current_time, new_next_hop, new_cost))
							cost_diff = new_cost - old_cost
							indicator = "↓" if cost_diff < 0 else "↑"
							print(f"[{node_id}] CUSTO p/ {dest_id}: {old_cost:.0f}ms → {new_cost:.0f}ms [{indicator}{abs(cost_diff):.0f}ms]")
					else:
						# Nova rota descoberta
						route_history[node_id][dest_id].append((current_time, new_next_hop, new_cost))

				# Atualizar dados principais
				node_state[node_id]["last_seen"] = current_time
				node_state[node_id]["neighbors"] = new_neighbors
				node_state[node_id]["routing_table"] = new_routing_table
				node_state[node_id]["streams"] = update.get("streams", [])

		except Exception:
			# Ignora erros temporários para manter o loop vivo
			time.sleep(0.1)
			continue


def display_monitor():
	"""Thread que mostra tabelas de estado periodicamente."""
	while True:
		time.sleep(3)

		with state_lock:
			if not node_state:
				continue

			# Limpar tela (ANSI escape code)
			print("\033[2J\033[H", end="")

			now = time.time()
			print("=" * 120)
			print(f"MONITORIZAÇÃO OVERLAY NETWORK - {datetime.now().strftime('%H:%M:%S')}")
			print("=" * 120)

			# Tabela de nós ativos
			print("\nNÓS ATIVOS:")
			print(f"{'ID':<10} {'Estado':<7} {'Última Conexão':<16} {'Tempo':<8} {'Vizinhos Ativos':<40}")
			for node_id in sorted(node_state.keys()):
				info = node_state[node_id]
				last_seen_seconds = now - info.get("last_seen", 0)
				status = "ALIVE" if last_seen_seconds < 10 else ("LOST" if last_seen_seconds < 30 else "DEAD")
				last_conn = datetime.fromtimestamp(info.get("last_seen", 0)).strftime('%H:%M:%S') if info.get("last_seen") else "N/A"
				time_elapsed = f"{int(last_seen_seconds)}s" if last_seen_seconds < 60 else f"{int(last_seen_seconds/60)}m"
				neighbors = info.get('neighbors', {})

				active_neighbors = []
				# Itera sobre o IP e o custo direto para esse vizinho
				for neighbor_ip, cost_to_neighbor in neighbors.items():
					# Verificar se este IP pertence a algum nó que está no node_state e está vivo
					for other_node_id, other_info in node_state.items():
						if other_info.get('ip') == neighbor_ip:
							other_last_seen = now - other_info.get('last_seen', 0)
							if other_last_seen < 30:
								# Agora inclui o custo da ligação na visualização
								try:
									active_neighbors.append(f"{other_node_id}({float(cost_to_neighbor):.0f}ms)")
								except Exception:
									active_neighbors.append(f"{other_node_id}({cost_to_neighbor})")
							break

				neighbors_str = ', '.join(sorted(active_neighbors)) if active_neighbors else "-"
				print(f"{node_id:<10} {status:<7} {last_conn:<16} {time_elapsed:<8} {neighbors_str:<40}")

			# Tabela de rotas
			print("\nTABELA DE ROTAS:")
			print(f"{'Nó':<10} {'Destino':<12} {'Próx. Salto':<16} {'Custo':<8} {'Downstream':<10}")
			for node_id in sorted(node_state.keys()):
				routing_table = node_state[node_id].get('routing_table', {})
				for dest_id, route_info in sorted(routing_table.items()):
					next_hop = route_info.get('next_hop', 'N/A')
					cost = route_info.get('cost', 0)
					downstream_count = len(route_info.get('downstream', []))
					print(f"{node_id:<10} {dest_id:<12} {next_hop:<16} {cost:>6.1f}ms {downstream_count:<10}")

			# Streams ativos
			print("\n┌─── STREAMS ATIVOS " + "─" * 98 + "┐")
			stream_info = defaultdict(list)
			for node_id, info in node_state.items():
				for stream in info.get('streams', []):
					stream_info[stream].append(node_id)

			if stream_info:
				for stream_id, nodes in stream_info.items():
					streamers = [n for n in nodes if 'STREAMER' in n]
					clients = [n for n in nodes if 'C' in n]
					routers = [n for n in nodes if n not in streamers and n not in clients]

					print(f"│ Stream: {stream_id:<30} Streamer: {','.join(streamers) if streamers else 'N/A':<20} Clients: {len(clients):<5} Routers: {len(routers):<5} │")
			else:
				print(f"│ {'Nenhum stream ativo':<117} │")

			print("└" + "─" * 119 + "┘")
			print("\n[Pressione Ctrl+C para parar o tracker]")


def start_tracker():
	load_topology()  # Inicializa o modo dinâmico

	# Servidor TCP para bootstrap
	server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Permite reuso rápido do endereço
	server.bind((BIND_IP, BIND_PORT))
	server.listen(5)
	print(f"[*] Bootstrapper TCP a correr em {BIND_IP}:{BIND_PORT}")

	# Servidor UDP para monitorização
	monitor_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	monitor_sock.bind((BIND_IP, MONITOR_PORT))
	print(f"[*] Monitor UDP a correr em {BIND_IP}:{MONITOR_PORT}")

	# Iniciar threads
	threading.Thread(target=handle_state_update, args=(monitor_sock,), daemon=True).start()
	threading.Thread(target=display_monitor, daemon=True).start()

	print(f"[*] Sistema de monitorização ativo. Aguardando registo e updates dos nós...")

	while True:
		try:
			client, addr = server.accept()
			threading.Thread(target=handle_client, args=(client, addr), daemon=True).start()
		except KeyboardInterrupt:
			# Captura no loop principal
			raise


if __name__ == "__main__":
	try:
		start_tracker()
	except KeyboardInterrupt:
		print("\n\n[*] Tracker encerrado pelo utilizador.")
		sys.exit(0)