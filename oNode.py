import socket
import threading
import json
import time
import subprocess
from collections import defaultdict
from protocol import *



REQ_PORT = 3001
STR_PORT = 3002
FLO_PORT = 3010
HRT_PORT = 3011


STR_PORT_START = 4000
STR_PORT_LIMIT = 4050


CONFIG_FILE = "bootstrap_conf.json"
CLEAR_INT = 4.5
FLOOD_TIMEOUT = 10
HEARTBEAT_INTERVAL = 2
HEARTBEAT_TIMEOUT = 5
INITIAL_WAIT_FOR_HEARTS = 5



class oNode:
    def __init__(self, id_node, neighbours_table, neighbour, is_boot, is_pop):
        self.id = id_node # identificador str
        self.neighbours_table = neighbours_table # [{"IP": ip, "Latency": 0, "State": False}]
        self.activeStreams = {} # {nome_stream: { "clientes": [(addr, socket)],"thread": thread, "socket": socket, "stop": True/False, "clients_sockets": [(addr, socket)]}
        self.clients_last_heartbeat = {}  
        self.lock = threading.Lock()
        self.received_floods = set()

        self.bootstrapper = is_boot

        self.pop = is_pop
        self.port_dic = {} # {"video": (porta, true/false)} range 4000 -> 4050
    

    def clear_stream_port(self, video):
        if video in self.port_dic:
            del self.port_dic[video]
        else:
            print(f"Video '{video}' not found in port dictionary.")



    def get_an_available_port(self, video):

        if video in self.port_dic:
            return self.port_dic[video]

        used_ports = set(self.port_dic.values())
        for port in range(STR_PORT_START, STR_PORT_LIMIT + 1):
            if port not in used_ports:
                self.port_dic[video] = port
                return port

        raise Exception("No available ports in the range.")

    def heartbeat_listener(self):
        def listen_for_heartbeats():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
                server_socket.bind(("0.0.0.0", HRT_PORT))
                server_socket.listen()
                print(f"Heartbeats listening on port {HRT_PORT}...")

                while True:
                    client_socket, addr = server_socket.accept()
                    data_r = client_socket.recv(1024)
                    request = Message.deserialize(data_r)
                    if request.type == Message.HEARTBEAT:
                        #print(f"heartbeat from {addr[0]}")
                        self.clients_last_heartbeat[addr[0]] = time.time()
                    client_socket.close()

        def send_heartbeats():
            while True:
                neighbour_ip = self.get_best_neighbour_ip()
                heartbeat_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                if neighbour_ip:
                    try:
                        heartbeat_socket.connect((neighbour_ip, HRT_PORT))
                        heartbeat_msg = Message(type=Message.HEARTBEAT).serialize()
                        #print(f"Sending heartbeat to {neighbour_ip}...")
                        heartbeat_socket.send(heartbeat_msg)  
                    except Exception as e:
                        print(f"Error sending heartbeat: {e}")
                    finally:
                        heartbeat_socket.close() 

                time.sleep(HEARTBEAT_INTERVAL)

        listener_thread = threading.Thread(target=listen_for_heartbeats, daemon=True)
        listener_thread.start()

        sender_thread = threading.Thread(target=send_heartbeats, daemon=True)
        sender_thread.start()

        listener_thread.join()
        sender_thread.join()



    def clean_floods_and_check_neighbours(self):
        initial_wait = True  
        last_heartbeat_check_time = time.time() 

        while True:
            current_time = time.time()

            with self.lock:
                self.received_floods.clear()
                #print("Cleared received_floods list.")

                for neighbour in self.neighbours_table:
                    last_seen = neighbour.get("last_seen", 0)
                    if current_time - last_seen > FLOOD_TIMEOUT:
                        if neighbour["State"]:
                            print(f"Neighbour {neighbour['IP']} marked as inactive.")
                            neighbour["State"] = False

            if initial_wait:
                if current_time - last_heartbeat_check_time > INITIAL_WAIT_FOR_HEARTS:
                    initial_wait = False 
                    print("Starting to inspect heartbeats")
                    last_heartbeat_check_time = current_time  

            if not initial_wait:
                with self.lock:
                    active_streams_copy = list(self.activeStreams.items())

                    if len(active_streams_copy) > 0:
                        for video, stream_data in active_streams_copy:
                            for client_addr, _ in stream_data["clients"]:
                                try:
                                    if time.time() - self.clients_last_heartbeat[client_addr[0]] > HEARTBEAT_TIMEOUT:
                                        print(f"Client {client_addr[0]} missed heartbeat. Closing his stream...")
                                        msg = Message(type=Message.STOP_VIDEO, data={"video": video}).serialize()
                                        self.handle_stop_request(msg, (client_addr[0], 0))
                                        del self.client_last_heartbeat[client_addr[0]]


                                except Exception as e:
                                    print(f"Heartbeat Monotoring stopped.")

            time.sleep(CLEAR_INT)




    def handle_new_node(self, client_socket, addr):
        # Bootstrap handling removed from node. Use external bootstrap service (bootstrap_inicio.py).
        try:
            # No-op placeholder to gracefully close incoming connection
            _ = client_socket.recv(1)
        except Exception:
            pass
        finally:
            client_socket.close()

    def sort_neighbours_table(self):
        #print("Sorting neighbours table...")
        #print(self.neighbours_table)
        return sorted(self.neighbours_table, key=lambda x: x["Latency"])

    def mark_neighbour_inactive(self, neighbour_ip):
        for neighbour in self.neighbours_table:
            if neighbour["IP"] == neighbour_ip:
                neighbour["State"] = False
                neighbour["Latency"] = float('inf')
                print(f"Neighbour {neighbour_ip} marked as inactive.")
                break

    def get_best_neighbour_ip(self):
        #print("Getting best neighbour from table...")
        for neighbour in self.neighbours_table:
            if neighbour["State"]:
                ip = neighbour["IP"]
                #print(f"BEST NEIGHBOUR -> {ip}")
                return neighbour["IP"]
        return None

    def flood_listener(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("0.0.0.0", FLO_PORT))
            sock.listen()
            print(f"Node {self.id} listening for flood messages on port {FLO_PORT}...")

            while True:
                client_socket, addr = sock.accept()
                threading.Thread(target=self.handle_flood_packet, args=(client_socket, addr), daemon=True).start()

    def handle_flood_packet(self, client_socket, addr):
        try:
            data = client_socket.recv(1024)
            flood_packet = Message.deserialize(data)

            if flood_packet.type == Message.FLOOD:
                #print(f"Received flood packet from {addr[0]}")
                with self.lock:

                    for neighbour in self.neighbours_table:
                        if neighbour["IP"] == addr[0]:
                            neighbour["last_seen"] = time.time()  # Atualiza o timestamp
                            break
                    if addr[0] not in self.received_floods:
                        self.received_floods.add(addr[0])

                        rtt = time.time() - flood_packet.data
                        self.update_latency(addr[0], rtt)

                        if not (self.pop):
                            self.forward_flood(flood_packet, addr[0])

                self.sort_neighbours_table()
        except Exception as e:
            print(f"Error handling flood packet from {addr}: {e}")  
        finally:
            client_socket.close()


    def update_latency(self, neighbour_ip, latency):
        for neighbour in self.neighbours_table:
            if neighbour["IP"] == neighbour_ip:
                neighbour["Latency"] = latency
                neighbour["State"] = True
                break


    def forward_flood(self, flood_packet, ip):
        for neighbour in self.neighbours_table:
            if neighbour["IP"] != ip and neighbour["IP"] not in self.received_floods:
                ip_sent = neighbour["IP"]
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                        sock.settimeout(2)
                        sock.connect((ip_sent, FLO_PORT))
                        sock.sendall(Message.serialize(flood_packet))
                        #print(f"Sent FLOOD packet to {ip_sent}.")
                except (socket.error, ConnectionRefusedError) as e:
                    print(f"Failed to send FLOOD packet to {ip_sent}: {e}. Skipping.")


    def handle_check_videos(self, data, c_socket, requester):
        try:
            message = Message.deserialize(data)
            if message.type == Message.CHECK_ALL_VIDEOS:
                # Conecta ao vizinho via TCP para encaminhar a requisição
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as neighbour_socket:
                    neighbour = self.get_best_neighbour_ip()
                    neighbour_socket.connect((neighbour, REQ_PORT))
                    print(f"Forwarding CHECK_ALL_VIDEOS request to neighbour {neighbour}...")

                    neighbour_socket.sendall(data)  # Encaminha a requisição ao vizinho

                    # Recebe a resposta do vizinho
                    answer = neighbour_socket.recv(1024)
                    print(f"Received answer from neighbour {neighbour}. Forwarding to {requester}.")

                    # Encaminha a resposta ao cliente original
                    c_socket.sendall(answer)

            else:
                print("Handler answering wrong type request.")
        except Exception as e:
            print(f"Error in handle_check_videos: {e}")

        finally:
            print("Request socket closed.")


    def handle_start_video(self, msg, requester, client_socket):


        message = Message.deserialize(msg)
        type = message.type
        data = message.data
        video = data['video']
        port = data['port']

        if type == Message.START_VIDEO:
            neighbour = self.get_best_neighbour_ip()
            if video in self.activeStreams:

                clients = [client[0] for client in self.activeStreams[video]["clients"]]

                if requester in clients:
                    print("Client already receiving that stream")

                else:
                    self.add_new_client_to_stream(video, requester, client_socket, port)

            else:
                print(f"Stream '{video}' not active. Requesting from neighbour {neighbour}")
                self.request_stream_from_neighbour(video, requester, client_socket, port)

    def add_new_client_to_stream(self, video, requester, client_socket, port):
        new_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.activeStreams[video]["clients"].append(((requester[0], port), new_socket))
        self.activeStreams[video]["clients_sockets"].append(((requester[0], REQ_PORT), client_socket))
        response = Message(type=Message.ACK)
        client_socket.send(response.serialize())
        print(f"Added client {requester} to stream '{video}'.")


    def request_stream_from_neighbour(self, video, requester, client_socket, port_s, neighbour=None):
        try:
            if not neighbour:
                neighbour = self.get_best_neighbour_ip()

            while neighbour:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as neighbour_socket:
                        neighbour_socket.settimeout(5)
                        neighbour_socket.connect((neighbour, REQ_PORT))
                        print(f"Connected to neighbour {neighbour} on port {REQ_PORT}.")
                        port_r = self.get_an_available_port(video)
                        print(f"requesting to get stream on port: {port_r}")


                        start_video_msg = Message(Message.START_VIDEO, data={"video": video, "port": port_r}).serialize()
                        neighbour_socket.sendall(start_video_msg)
                        print(f"Requested stream '{video}' from neighbour {neighbour}.")

                        response_data = neighbour_socket.recv(1024)
                        response_msg = Message.deserialize(response_data)

                        if response_msg.type == Message.ACK:
                            print(f"ACK received for stream '{video}' from neighbour {neighbour}. Starting retransmission...")
                            if client_socket.fileno() != -1:
                                client_socket.sendall(response_data)
                            else:
                                print("Error: Attempted to send to a closed socket.")


                            stream_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                            stream_socket.bind(("0.0.0.0", port_r))

                            self.activeStreams[video] = {
                                "clients": [((requester[0], port_s), socket.socket(socket.AF_INET, socket.SOCK_DGRAM))],
                                "thread": None,
                                "socket": stream_socket,
                                "clients_sockets": [((requester[0], REQ_PORT), client_socket)],
                                "stop": False,  
                            }

                            self.start_retransmission(video, stream_socket, requester, client_socket, port_s)
                            return

                        elif response_msg.type == Message.ERROR:
                            print(f"Unexpected ERROR: {response_msg.data} from neighbour {neighbour}.")
                            client_socket.sendall(response_data)
                            return

                except (socket.timeout, ConnectionError) as e:
                    print(f"Neighbour {neighbour} failed. Marking as inactive: {e}")
                    self.mark_neighbour_inactive(neighbour)
                    neighbour = self.get_best_neighbour_ip()

            print("No active neighbours available for stream.")
            error_response = Message(Message.ERROR, data="No active neighbours available.").serialize()
            client_socket.sendall(error_response)

        except Exception as e:
            print(f"Error requesting stream '{video}': {e}")
            error_response = Message(Message.ERROR, data=f"Stream error: {e}").serialize()
            client_socket.sendall(error_response)


    def start_retransmission(self, video, stream_socket, requester, client_socket, port_s):
        buffer = []
        buffer_size = 10
        timeout_duration = 0.5


        def retransmit():
            switching_stream = False
            try:

                last_received_time = time.time()
                while not self.activeStreams[video]["stop"]:
                    
                    try:
                        stream_socket.settimeout(2) 
                        data, _ = stream_socket.recvfrom(65536)
                        
                        last_received_time = time.time() 
                        buffer.append(data)
                        if len(buffer) > buffer_size:
                            buffer.pop(0)

                        for client_addr, client_sock in self.activeStreams[video]["clients"]:
                            client_sock.sendto(buffer[0], client_addr)
                            buffer.pop(0)
                            print(f"Retransmitted packet to {client_addr}")



                    except socket.timeout:
                        print(f"node was listening for {video} on: {port_s}")
                        print(f"Socket timeout detected at {time.time()}. Last data received at {last_received_time}")
                        if not self.activeStreams[video]["stop"]:
                            if time.time() - last_received_time > timeout_duration:
                                print(f"Timeout detected for stream '{video}'. Switching to next neighbour.")
                                switching_stream = True
                                
                                # for addr, sock in self.activeStreams[video]["clients_sockets"]:
                                #     print(f"addr[0]: {addr[0]}, requester[0]: {requester[0]}")

                                stream_socket.close()
                                self.switch_to_next_neighbour(video, requester, client_socket, port_s)
                                break

                    print(f"Sent heartbeat for stream '{video}'.")

            except Exception as e:
                print(f"...")
                if video in self.activeStreams and self.activeStreams[video] is not None:
                    self.activeStreams[video]["stop"] = True

            finally:
                if not switching_stream:
                    if video in self.activeStreams and self.activeStreams[video] is not None:
                        for _, sock in self.activeStreams[video]["clients"]:
                            sock.close()
                        stream_socket.close()
                        if video in self.activeStreams and self.activeStreams[video] is not None:
                            del self.activeStreams[video]
                        print(f"Stopped retransmitting stream '{video}'.")

        retransmission_thread = threading.Thread(target=retransmit, daemon=True)
        self.activeStreams[video]["thread"] = retransmission_thread
        retransmission_thread.start()
        print(f"Started retransmission for stream '{video}'.")

    def switch_to_next_neighbour(self, video, requester, client_socket, port_s):

        current_neighbour = self.get_best_neighbour_ip()
        self.mark_neighbour_inactive(current_neighbour)
        second_best_neighbour = self.get_best_neighbour_ip()

        
        clients_snapshot = list(self.activeStreams[video]["clients_sockets"])

        if second_best_neighbour:
            print(f"Switching stream '{video}' to second best neighbour")
            self.request_stream_from_neighbour(video, requester, client_socket, port_s, second_best_neighbour)
            
            for addr, sock in clients_snapshot:
                print(f"addr to swap: {addr[0]} alse the requester: {requester[0]}")
                if addr[0] != requester[0]:

                    self.add_new_client_to_stream(video, addr, sock, addr[1])
                    
        else:
            print(f"No second best neighbour available for stream '{video}'. Stopping stream.")
            self.cleanup_stream(video)


    def cleanup_stream(self, video):

        if video in self.activeStreams:
            stream_socket = self.activeStreams[video]["socket"]
            for _, client_sock in self.activeStreams[video]["clients"]:
                client_sock.close()
            stream_socket.close()
            del self.activeStreams[video]
            print(f"Stream '{video}' fully stopped.")


    def handle_stop_request(self, msg, requester):

        message = Message.deserialize(msg)
        video = message.data["video"]

        if video not in self.activeStreams:
            print(f"Stream '{video}' not active. Ignoring STOP request from {requester}.")
            return
        
        clients = self.activeStreams[video]["clients"]
        client_index = next((i for i, (addr,_) in enumerate(clients) if addr[0] == requester[0]), None)

        if client_index is not None:
            _, client_sock = self.activeStreams[video]["clients"].pop(client_index)
            client_sock.close()
            print(f"Client {requester} removed from stream '{video}'.")

            if not self.activeStreams[video]["clients"]:
                print(f"No more clients for stream '{video}'. Stopping stream.")
                self.stop_stream(video)

        else:
            print(f"Client {requester} is not receiving stream '{video}'. Ignoring STOP request.")


    def stop_stream(self, video):
        if video not in self.activeStreams:
            print(f"Stream '{video}' not active. Nothing to stop.")
            return

        print(f"Stopping stream '{video}'...")

        stream_socket = self.activeStreams[video]["socket"]
        ret_thread = self.activeStreams[video]["thread"]
        neighbour = self.get_best_neighbour_ip()

        if neighbour:
            stop_video_msg = Message(Message.STOP_VIDEO, data={"video": video}).serialize()
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as neighbour_socket:
                    neighbour_socket.connect((neighbour, REQ_PORT))
                    neighbour_socket.sendall(stop_video_msg)
                    print(f"Notified neighbour {neighbour} to stop stream '{video}'.")
            except Exception as e:
                print(f"Failed to notify neighbour to stop stream '{video}': {e}")

        if ret_thread and ret_thread.is_alive():
            self.activeStreams[video]["stop"] = True 
            ret_thread.join(timeout=1)
            print(f"Stopped retransmission thread for stream '{video}'.")

        stream_socket.close()
        neighbour_socket.close()
        if video in self.activeStreams and self.activeStreams[video] is not None:
            del self.activeStreams[video]
        print(f"Stream '{video}' fully stopped.")


    def handle_requests(self):

        req_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        req_socket.bind(("0.0.0.0", REQ_PORT))
        req_socket.listen()
        print(f"Node listening on port {REQ_PORT} for TCP requests...")

        try:
            while True:
                # Aceita conexão do cliente
                client_socket, addr = req_socket.accept()
                print(f"Accepted connection from {addr}.")

                threading.Thread(
                    target=self.handle_client_request, 
                    args=(client_socket, addr), 
                    daemon=True
                ).start()
            
        except Exception as e:
            print(f"Error in main request loop: {e}")
        finally:
            print("Request socket closed.")

    def handle_client_request(self, client_socket, addr):
        try:
            # Recebe dados do cliente
            data = client_socket.recv(1024)
            if not data:
                print(f"Connection ended without data from {addr}.")
                return

            print(f"Received request from {addr}.")
            message = Message.deserialize(data)

            # Processa o tipo de mensagem recebido
            if message.type == Message.CHECK_ALL_VIDEOS:
                self.handle_check_videos(data, client_socket, addr)
            elif message.type == Message.START_VIDEO:
                self.handle_start_video(data, addr, client_socket)
            elif message.type == Message.STOP_VIDEO:
                self.handle_stop_request(data, addr)
            else:
                print(f"Unknown message type {message.type} from {addr}")
        except Exception as e:
            print(f"Error handling request from {addr}: {e}")
        finally:
            print(f"Finished processing request from {addr}. Socket will remain open for future communication.")

    def bs_get_own_neighbours(self):
        # Method removed: bootstrapper responsibilities handled by external bootstrap service.
        print("bs_get_own_neighbours is not available; use external bootstrap service.")
        return None


def get_bootstrapper_info(bootstrapper_ip, node_id):
    BOOTSTRAP_PORT = 5555
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect((bootstrapper_ip, BOOTSTRAP_PORT))
            payload = json.dumps({"id": node_id}).encode('utf-8')
            s.sendall(payload)

            data = s.recv(4096)
            if not data:
                print("No data received from bootstrapper.")
                return None

            try:
                resp = json.loads(data.decode('utf-8'))
            except Exception as e:
                print(f"Invalid JSON from bootstrapper: {e}")
                return None

            if resp.get('status') != 'OK':
                print(f"Bootstrapper returned error status: {resp.get('status')}")
                return None

            neighbours = resp.get('neighbors', [])
            neighbours_table = []

            for entry in neighbours:
                # `bootstrap_inicio.py` envia objetos com chave "ip" (ou apenas strings)
                ip = entry.get('ip') if isinstance(entry, dict) else entry
                if not ip:
                    continue
                neighbour_entry = {
                    "IP": ip,
                    "Latency": 0,
                    "State": False,
                    "last_seen": 0
                }
                neighbours_table.append(neighbour_entry)

            print("Neighbours table created:")
            for neighbour in neighbours_table:
                print(neighbour)

            return neighbours_table

    except Exception as e:
        print(f"Error contacting bootstrapper: {e}")
        return None

if __name__ == "__main__":
    import sys

    if len(sys.argv) != 4:
        print("Usage: python oNode.py <node_id> <node_type> <bootstrapper_ip>")
        sys.exit(1)

    node_id = sys.argv[1]
    node_type = sys.argv[2]
    bootstrapper_ip = sys.argv[3]
    # Integrated bootstrapper mode is no longer supported in this script.
    # Run `bootstrap_inicio.py` as a separate process if you need a bootstrap service.
    if int(node_type) == 1:
        print("Integrated bootstrapper not supported. Run 'bootstrap_inicio.py' and start node as type 2 or 3.")
        sys.exit(1)

    elif int(node_type) == 2:
        neighbours_t = get_bootstrapper_info(bootstrapper_ip, node_id)
        node = oNode(id_node=node_id, neighbours_table=neighbours_t, neighbour=None, is_boot=False, is_pop=True)
        try:
            node.init_node()
        except KeyboardInterrupt:
            print(f"Stopping node {node.id}...")
            for video, stream_info in node.activeStreams.items():
                node.stop_stream(video)

    else:
        neighbours_t = get_bootstrapper_info(bootstrapper_ip, node_id)
        if neighbours_t:
            node = oNode(id_node=node_id, neighbours_table=neighbours_t, neighbour=None, is_boot=False, is_pop=False)
            try:
                node.init_node()
            except KeyboardInterrupt:
                print(f"Stopping node {node.id}...")
                for video, stream_info in node.activeStreams.items():
                    node.stop_stream(video)
        else:
            print("Bootstrapper error.")
