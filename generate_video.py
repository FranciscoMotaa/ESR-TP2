import cv2
import sys
import os

def create_simulation_video(input_file, output_file, max_width=480, quality=50):
    """
    Converte um vídeo normal para o formato customizado do simulador:
    [5 bytes TAMANHO][DADOS JPEG][5 bytes TAMANHO][DADOS JPEG]...
    """
    if not os.path.exists(input_file):
        print(f"[ERRO] Ficheiro de entrada '{input_file}' não encontrado.")
        sys.exit(1)

    cap = cv2.VideoCapture(input_file)
    if not cap.isOpened():
        print("[ERRO] Não consegui abrir o vídeo.")
        sys.exit(1)

    print(f"[*] A converter '{input_file}' para '{output_file}'...")
    print(f"[*] Resolução alvo: {max_width}px largura (para manter pacotes pequenos)")

    try:
        with open(output_file, 'wb') as f_out:
            count = 0
            while True:
                ret, frame = cap.read()
                if not ret:
                    break

                # 1. Redimensionar (Crucial para UDP/Simulação)
                # Reduzimos para garantir que o frame cabe em pacotes UDP e não mata a rede
                height, width = frame.shape[:2]
                scale = max_width / width
                new_height = int(height * scale)
                frame_resized = cv2.resize(frame, (max_width, new_height))

                # 2. Codificar para JPEG
                encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
                result, encimg = cv2.imencode('.jpg', frame_resized, encode_param)
                
                if not result:
                    continue

                data = encimg.tobytes()
                size = len(data)

                # 3. Verificação de Segurança (O seu código lê 5 bytes, max 99999)
                if size > 99999:
                    print(f"[AVISO] Frame {count} é demasiado grande ({size} bytes). A ignorar.")
                    continue

                # 4. Criar o Cabeçalho de 5 bytes (Ex: 04096)
                # O seu código faz: framelength = int(data) onde data tem 5 bytes
                header = str(size).zfill(5).encode('utf-8')

                # 5. Escrever no ficheiro
                f_out.write(header)
                f_out.write(data)
                
                count += 1
                if count % 50 == 0:
                    print(f"\rProcessados: {count} frames...", end="")

        print(f"\n[SUCESSO] Vídeo gerado: {output_file} ({count} frames)")
        print("Agora configure o seu main.py para usar este ficheiro.")

    except Exception as e:
        print(f"\n[ERRO CRÍTICO] {e}")
    finally:
        cap.release()

if __name__ == "__main__":
    # USO: python generate_video.py input.mp4 movie.Mjpeg
    if len(sys.argv) < 3:
        print("Uso: python generate_video.py <input.mp4> <output.Mjpeg>")
    else:
        create_simulation_video(sys.argv[1], sys.argv[2])