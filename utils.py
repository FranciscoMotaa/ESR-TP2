import socket
import fcntl
import struct

def get_interface_ip(ifname='eth0'):
    """
    Obtém o endereço IP real de uma interface de rede específica.
    Essencial no CORE porque gethostbyname devolve 127.0.0.1.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Chamada ioctl para obter o IP da interface (Linux specific)
        ip_bytes = fcntl.ioctl(
            s.fileno(),
            0x8915,  # SIOCGIFADDR
            struct.pack('256s', bytes(ifname[:15], 'utf-8'))
        )[20:24]
        return socket.inet_ntoa(ip_bytes)
    except Exception as e:
        # Fallback para debug fora do CORE
        print(f"[WARN] Não foi possível obter IP da {ifname}: {e}")
        return "127.0.0.1"
