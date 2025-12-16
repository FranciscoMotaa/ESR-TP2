import socket
import fcntl
import socket
import fcntl
import struct
import subprocess
import re


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
    except Exception:
        # Fallback para debug fora do CORE
        try:
            out = subprocess.check_output(['hostname', '-I']).decode('utf-8').strip()
            if out:
                # Escolher o primeiro IP que não seja loopback
                for ip in out.split():
                    if not ip.startswith('127.'):
                        return ip
                return out.split()[0]
        except Exception:
            pass
        return "127.0.0.1"


def measure_rtt(host: str, count: int = 1, timeout: int = 1) -> float:
    """Tenta medir RTT ao `host` usando o utilitário `ping` do sistema.
    Retorna o tempo em ms (float) ou None se falhar.
    """
    try:
        # -c count, -W timeout (seconds) para espera por reply
        p = subprocess.run(['ping', '-c', str(count), '-W', str(timeout), host], capture_output=True, text=True, timeout=timeout+1)
        out = p.stdout
        # Procura por 'time=X ms'
        m = re.search(r'time=([0-9]+\.?[0-9]*)\s*ms', out)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None