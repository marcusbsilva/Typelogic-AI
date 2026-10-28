"""Local IPC only; no downloads, DNS or outbound connections in application Python."""
import os
import socket
import ipaddress
_installed = False

def allowed(address):
    if not isinstance(address, tuple): return True
    try: return ipaddress.ip_address(str(address[0])).is_loopback
    except ValueError: return str(address[0]).lower() == 'localhost'

def install():
    global _installed
    for key in ('ORT_DISABLE_TELEMETRY','HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE','HF_DATASETS_OFFLINE','HF_HUB_DISABLE_TELEMETRY','DO_NOT_TRACK'):
        os.environ[key] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    if _installed: return
    original = socket.socket
    class LocalSocket(original):
        def connect(self,address):
            if not allowed(address): raise PermissionError('Offline: external connection blocked')
            return super().connect(address)
        def connect_ex(self,address):
            if not allowed(address): raise PermissionError('Offline: external connection blocked')
            return super().connect_ex(address)
        def sendto(self,data,*args):
            if args and not allowed(args[-1]): raise PermissionError('Offline: envio externo bloqueado')
            return super().sendto(data,*args)
    socket.socket = LocalSocket
    getaddrinfo = socket.getaddrinfo
    def local_dns(host,*args,**kwargs):
        if host is not None and not allowed((host,0)): raise PermissionError('Offline: DNS externo bloqueado')
        return getaddrinfo(host,*args,**kwargs)
    socket.getaddrinfo = local_dns
    _installed = True
