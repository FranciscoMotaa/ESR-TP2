import socket
import fcntl
import struct


def _ip_via_ioctl(ifname):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        ip_bytes = fcntl.ioctl(
            s.fileno(),
            0x8915,  # SIOCGIFADDR
            struct.pack('256s', bytes(ifname[:15], 'utf-8'))
        )[20:24]
        return socket.inet_ntoa(ip_bytes)
    except Exception:
        return None


def _ip_via_udp_probe():
    """Detecta um IP não-loopback criando um socket UDP e ligando a um destino público.
    Não envia pacotes; apenas permite obter o IP local usado para a rota.
    Funciona bem quando existe uma rota padrão configurada (inclui CORE se roteado).
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        # IP público qualquer; não é necessário ser alcançável
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def get_interface_ip(ifname='eth0'):
    """Obtém o endereço IP real de uma interface.

    Estratégia:
    1. Tenta obter via ioctl na interface `ifname` (uso em CORE se souberes a interface).
    2. Tenta detetar um IP não-loopback usando um socket UDP para um endereço público.
    3. Tenta algumas interfaces comuns ('eth0','eth1','ens3','enp0s3').
    4. Por fim devolve '127.0.0.1' como último recurso.
    """
    ip = _ip_via_ioctl(ifname)
    if ip:
        return ip

    ip = _ip_via_udp_probe()
    if ip:
        return ip

    for candidate in ('eth0', 'eth1', 'ens3', 'enp0s3'):
        ip = _ip_via_ioctl(candidate)
        if ip:
            return ip

    print(f"[WARN] Não foi possível detectar IP da interface; a usar 127.0.0.1")
    return '127.0.0.1'