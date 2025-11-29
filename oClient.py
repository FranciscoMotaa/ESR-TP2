import socket
import subprocess
import threading
import sys
import logging
import time

import cv2
import numpy as np
from protocol import *

REQ_PORT = 3001
STR_PORT = 3002
HRT_PORT = 3011


HEARTBEAT_INTERVAL = 1



class oClient:
    def __init__(self, node_ip):

        self.node_ip = node_ip
        self.active_stream = None 
        self.receiving = False
        self.receive_thread = None

    def list_streams(self):

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_socket:
            try:
                print("Requesting available streams...")
                client_socket.connect((self.node_ip, REQ_PORT))

                request_msg = Message(type=Message.CHECK_ALL_VIDEOS).serialize()
                client_socket.sendall(request_msg)

                response_data = client_socket.recv(1024)
                response_msg = Message.deserialize(response_data)

                if response_msg.type == Message.A_CHECK_ALL_VIDEOS:
                    videos = response_msg.data["videos"]
                    print("\nAvailable streams:")
                    for video in videos:
                        print(f"- {video}")
                    print("")
                else:
                    print(f"Unexpected response: {response_msg.type}")
            except Exception as e:
                print(f"Error requesting streams: {e}")

    def start_stream(self, video):

        if self.active_stream is not None:
            print(f"Already receiving a stream: '{self.active_stream}'. Stop it first.")
            return

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_socket:
            try:
                print(f"Requesting to start stream '{video}'...")
                client_socket.connect((self.node_ip, REQ_PORT))

                request_msg = Message(type=Message.START_VIDEO, data={"video": video, "port": STR_PORT}).serialize()
                client_socket.sendall(request_msg)

                response_data = client_socket.recv(1024)
                response_msg = Message.deserialize(response_data)

                if response_msg.type == Message.ACK:
                    print("ACK packet received")
                    print(f"Stream '{video}' started. Receiving...")
                    self.active_stream = video
                    self.start_receiving()
                elif response_msg.type == Message.ERROR:
                    print(f"Error: {response_msg.data}")
                else:
                    print(f"Unexpected response: {response_msg.type}")
            except Exception as e:
                print(f"Error starting stream '{video}': {e}")

    def stop_stream(self):

        if self.active_stream is None:
            print("No active stream to stop.")
            return

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_socket:
            try:
                print(f"Requesting to stop stream '{self.active_stream}'...")
                client_socket.connect((self.node_ip, REQ_PORT))

                stop_msg = Message(type=Message.STOP_VIDEO, data={"video": self.active_stream}).serialize()
                client_socket.sendall(stop_msg)

                print(f"Stop request for stream '{self.active_stream}' sent.")
                self.stop_receiving()
            except Exception as e:
                print(f"Error stopping stream '{self.active_stream}': {e}")

    def start_receiving(self):

        self.receiving = True

        def receive():
            try:
                ffplay_cmd = [
                    "ffplay",
                    "-fflags", "nobuffer",
                    "-analyzeduration", "100000",
                    "-probesize", "500000",
                    "-flags", "low_delay",
                    "-framedrop",
                    "-sync", "ext",
                    "-i", f"udp://@:{STR_PORT}",
                    "-autoexit",
                    "-hide_banner",
                    "-loglevel", "error",
                ]

                # Inicia o processo do ffplay
                logging.info(f"Starting ffplay to listen on UDP port {STR_PORT}")
                ffplay_process = subprocess.Popen(
                    ffplay_cmd,
                    stderr=subprocess.PIPE,
                    stdout=subprocess.PIPE
                )




                ffplay_process.wait()
                logging.info("FFplay process finished.")
            except Exception as e:
                    logging.error(f"Error in receiving stream: {e}")

        self.receive_thread = threading.Thread(target=receive, daemon=True)
        self.receive_thread.start()

    def stop_receiving(self):
        self.receiving = False

        if hasattr(self, 'ffplay_process'):
            try:
                if self.ffplay_process.poll() is None:
                    self.ffplay_process.terminate()
                    self.ffplay_process.wait(timeout=5)
                    print("Stream stopped successfully.")
                else:
                    print("FFplay process is already terminated.")
            except Exception as e:
                print(f"Error stopping ffplay process: {e}")
        else:
            print("No active ffplay process to stop.")

        if self.receive_thread:
            self.receive_thread.join()
            self.receive_thread = None

        self.active_stream = None

    def send_heartbeats(self):
        neighbour_ip = self.node_ip

        while True:
            if not self.receiving:
                # Espera enquanto o recebimento está pausado
                time.sleep(1)
                continue

            try:
                # Cria um novo socket para cada envio
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as heartbeat_socket:
                    heartbeat_socket.settimeout(5)
                    heartbeat_socket.connect((neighbour_ip, HRT_PORT))

                    logging.info("Sending heartbeat...")
                    heartbeat_msg = Message(type=Message.HEARTBEAT)
                    heartbeat_socket.send(Message.serialize(heartbeat_msg))  # Envia o heartbeat

                time.sleep(HEARTBEAT_INTERVAL)  # Intervalo entre heartbeats
            except Exception as e:
                logging.error(f"Error sending heartbeat: {e}")

    def main(self):

        heartbeat_thread = threading.Thread(target=self.send_heartbeats, daemon=True)
        heartbeat_thread.start()

        while True:
            print("\nOptions:")
            print("1 - List available streams")
            print("2 - Start a stream")
            print("3 - Stop current stream")
            print("4 - Exit")
            choice = input("Choose an option: ")

            if choice == "1":
                self.list_streams()
            elif choice == "2":
                video = input("Enter the stream name: ")
                self.start_stream(video)
            elif choice == "3":
                self.stop_stream()
            elif choice == "4":
                print("Exiting...")
                self.stop_stream()
                break
            else:
                print("Invalid option. Try again.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python client.py <node_ip>")
        sys.exit(1)

    node_ip = sys.argv[1]
    client = oClient(node_ip)
    try:
        client.main()
    except KeyboardInterrupt:
        print("\nClient terminated.")