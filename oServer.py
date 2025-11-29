import socket
import subprocess
import threading
import sys
import json
from collections import defaultdict

import time
from protocol import *


REQ_PORT = 3001
STR_PORT = 3002
BOT_PORT = 3003
FLO_PORT = 3010
HRT_PORT = 3011

FLOOD_GAP = 5
HEARTBEAT_TIMEOUT = 5



class oServer:

    def __init__(self, neighbours_t, videos_list):
        self.neighbours_table = neighbours_t
        self.videos_list = videos_list
        self.activeStreams = {} # {nome_stream: { "clients": [(addr, socket)],"thread": thread, "proccess": ffmpeg,  "socket": socket} 
        self.clients_last_heartbeat = {} # {"ip": tts}


    def heartbeat_listener(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
            server_socket.bind(("0.0.0.0", HRT_PORT))
            server_socket.listen()
            #print(f"Heartbeats listening on port {HRT_PORT}...")

            while True:
                client_socket, addr = server_socket.accept()
                data_r = client_socket.recv(1024)
                request = Message.deserialize(data_r)
                if request.type == Message.HEARTBEAT:
                    #print(f"heartbeat from {addr[0]}")
                    self.clients_last_heartbeat[addr[0]] = time.time()

    def receive_heartbeats(self):
        time.sleep(7)
        while True:
            active_streams_copy = list(self.activeStreams.items())

            for video, stream_data in active_streams_copy:
                for client_addr, _ in stream_data["clients"]:
                    try:
                        if time.time() - self.clients_last_heartbeat[client_addr[0]] > HEARTBEAT_TIMEOUT:
                            print(f"Client {client_addr[0]} missed heartbeat. Closing his stream...")
                            self.stop_stream(video, (client_addr[0], 0))

                    except Exception as e:
                        print(f"Error in heartbeat monitoring: {e}")

            time.sleep(1)

    def start_stream(self, video, requester, port):

            if video in self.activeStreams:
                clients = [client[0] for client in self.activeStreams[video]["clients"]]

                if requester in clients:
                    print(f"Client {requester} already receiving that stream {video}")
                    return
                else:
                    print(f"Adding client {requester} to the active stream '{video}'.")
                    new_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    self.activeStreams[video]["clients"].append(((requester[0],port), new_socket))
                    return

            else:

                print(f"Starting a new stream '{video}' for client {requester}.")
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                video_path = f"videos/{video}"

                ffmpeg_cmd = [
                    "/usr/bin/ffmpeg",
                    "-re",
                    "-stream_loop", "-1",  # Infinite loop
                    "-i", video_path,      # Input video file
                    "-c:v", "libx264",     # H.264 encoding
                    "-preset", "ultrafast", # Low latency
                    "-tune", "zerolatency",
                    "-f", "mpegts",        # MPEG-TS format for streaming
                    "pipe:1",              # Output to stdout
                ]   



                ffmpeg_process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


                
                self.activeStreams[video] = {
                    "clients": [((requester[0],port), sock)],
                    "thread": None,
                    "process": ffmpeg_process,
                    "socket": sock
                }

                def transmit():
                    try:
                        while True:
                            packet = ffmpeg_process.stdout.read(65507)
                            if not packet:
                                break

                            for client_addr, client_sock in self.activeStreams[video]["clients"]:
                                client_sock.sendto(packet, client_addr)
                                print(f"Sent packet to {client_addr}.")
                            
                            time.sleep(0.001) 
                    except Exception as e:
                        print(f"....")
                    finally:
                        print(f"Stopping stream '{video}'.")
                        ffmpeg_process.terminate()
                        # if self.activeStreams[video]:
                        #     for _, client_sock in self.activeStreams[video]["clients"]:
                        #         client_sock.close()
                        #     sock.close()
                        #     del self.activeStreams[video]
                        print(f"Stream '{video}' fully shutdown.")

                retransmission_thread = threading.Thread(target=transmit, daemon=True)
                self.activeStreams[video]["thread"] = retransmission_thread
                retransmission_thread.start()

    def stop_stream(self, video, req):


        if video not in self.activeStreams:
            print(f"Stream '{video}' not active. Nothing to stop.")
            return


        stream_info = self.activeStreams[video]

        clients = self.activeStreams[video]["clients"]

        if isinstance(req, tuple):
            print(f"tuplo: {req}")
            requester = req[0]
        else:
            print(f"not tuplo: {req}")
            requester = req

        client_index = next((i for i, (addr,_) in enumerate(clients) if addr[0] == requester), None)

        if client_index is not None:
            _, client_sock = self.activeStreams[video]["clients"].pop(client_index)
            client_sock.close()
            print(f"Client {requester} removed from stream '{video}'.")

            if not self.activeStreams[video]["clients"]:
                print(f"No more clients for stream '{video}'. Stopping stream.")

                stream_socket = self.activeStreams[video]["socket"]
                ret_thread = self.activeStreams[video]["thread"]

                if ret_thread and ret_thread.is_alive():
                    self.activeStreams[video]["stop"] = True 
                    ret_thread.join(timeout=1)
                    print(f"Stopped retransmission thread for stream '{video}'.")

                stream_socket.close()
                del self.activeStreams[video]
                print(f"Stream '{video}' fully stopped.")

        else:
            print(f"...")

        
    def manage_requests(self, port):
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.bind(("0.0.0.0", port))
        server_socket.listen()
        print(f"Listening for requests on port {port}...")

        while True:
            client_socket, address = server_socket.accept()
            data = client_socket.recv(1024)
            threading.Thread(target=self.handle_request, args=(data, client_socket, address)).start()

    def handle_request(self, data, socket, address):
        try:
            message = Message.deserialize(data)
            requester = address[0]

            if message.type == Message.CHECK_ALL_VIDEOS:
                response = Message(type=Message.A_CHECK_ALL_VIDEOS, data={"videos": self.videos_list})
                socket.send(response.serialize())
                print(f"Responded with video list {self.videos_list} to {address}.")

            elif message.type == Message.START_VIDEO:
                video = message.data["video"]
                if video not in self.videos_list:
                    response = Message(type=Message.ERROR, data=f"Video '{video}' not found.")
                    socket.send(response.serialize())
                    print(f"Video '{video}' not found.")
                else:
                    port = message.data["port"]
                    print(f"streaming request on port {port}")

                    response = Message(type=Message.ACK)
                    print(f"ACK message sent")
                    socket.send(response.serialize())
                    self.start_stream(video, address, port)

            elif message.type == Message.STOP_VIDEO:
                video = message.data["video"]
                self.stop_stream(video, requester)

        except Exception as e:
            print(f"Error handling request from {address}: {e}")


    def flood_task(self):
        while True:
            try:
                flood_packet = Message.serialize(Message(type=Message.FLOOD, data=time.time()))


                for neighbour in self.neighbours_table:
                    ip = neighbour["IP"]
                    try:
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                            sock.settimeout(2)
                            sock.connect((ip, FLO_PORT))
                            sock.sendall(flood_packet)
                            print(f"Sent FLOOD packet to {ip}.")
                    except Exception as e:
                        print(f"Failed to send FLOOD packet to {ip}: {e}")
                time.sleep(FLOOD_GAP)

            except Exception as e:
                print(f"Error in flood_task: {e}")
                break


    

    def main(self):
        req_thread = threading.Thread(target=self.manage_requests, args=(REQ_PORT,))
        flood_thread = threading.Thread(target=self.flood_task, daemon=True).start()
        heartbeat_listener_thread = threading.Thread(target=self.heartbeat_listener, daemon=True).start()
        heartbeat_thread = threading.Thread(target=self.receive_heartbeats, daemon=True).start()
        req_thread.start()


def get_bootstrapper_info(bootstrapper_ip, node_id):

    try:
        bootstrapper_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bootstrapper_socket.connect((bootstrapper_ip, BOT_PORT))
        
        bootstrapper_socket.send(Message.serialize(Message(type=Message.REGISTER, data=node_id)))
        
        data = bootstrapper_socket.recv(1024)
        message = Message.deserialize(data)

        if message.type == Message.A_REGISTER:
            neighbours_list = message.data # por exemplo: ["192.10.3.1", "192.10.3.2", ...]
            neighbours_table = []

            for ip in neighbours_list:
                neighbour_entry = {
                    "IP": ip,           
                    "Latency": 0,     
                    "State": False 
                }
                neighbours_table.append(neighbour_entry)

            print("Neighbours table created:")
            for neighbour in neighbours_table:
                print(neighbour)

            return neighbours_table

        else:
            print("Wrong message type received. Aborting node initialization.")
            return None
    finally:
        bootstrapper_socket.close()


if __name__ == '__main__':
    neighbours = [] 
    videos_list = ["video1.mp4", "video2.mp4"]

    if len(sys.argv) != 3:
        print("Usage: python oServer.py <server_id> <bootstrapper_ip>")
        sys.exit(1)

    server_id = sys.argv[1]
    bootstrapper_ip = sys.argv[2]

    neighbours_t = get_bootstrapper_info(bootstrapper_ip, server_id)

    server = oServer(neighbours_t, videos_list)
    server.main()