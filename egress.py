"""Loopback egress proxy: resolve and pin public destinations before connecting.
Every downloader request, including HTTP redirects, passes through this proxy.
"""
import ipaddress
import select
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit


def connect_public(host, port):
    if port not in (80, 443):
        raise ValueError('Only public HTTP(S) media is permitted.')
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not answers:
        raise ValueError('Destination did not resolve.')
    for answer in answers:
        ip = ipaddress.ip_address(answer[4][0])
        if not ip.is_global or getattr(ip, 'ipv4_mapped', None):
            raise ValueError('Private or reserved network destination blocked.')
    last_error = None
    for family, kind, protocol, _, address in answers:
        connection = socket.socket(family, kind, protocol)
        connection.settimeout(25)
        try:
            # Connect to the resolved numeric address, never resolve again.
            connection.connect(address)
            return connection
        except OSError as error:
            last_error = error
            connection.close()
    raise last_error or OSError('Unable to connect')


class Proxy(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    def log_message(self, *args):
        pass
    def setup(self):
        super().setup()
        self.connection.settimeout(30)
    def tunnel(self, remote):
        try:
            while True:
                ready, _, _ = select.select([self.connection, remote], [], [], 45)
                if not ready:
                    break
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (remote if source is self.connection else self.connection).sendall(data)
        except (OSError, ValueError):
            pass
        finally:
            remote.close()
    def do_CONNECT(self):
        remote = None
        try:
            parsed = urlsplit('//'+self.path)
            if parsed.username or parsed.password or parsed.path:
                raise ValueError('Invalid destination')
            remote = connect_public(parsed.hostname, parsed.port or 443)
        except (OSError, ValueError, TypeError):
            self.send_error(403, 'Destination blocked or unavailable')
            return
        self.send_response(200, 'Connection established')
        self.end_headers()
        self.tunnel(remote)
    def do_GET(self):
        remote = None
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme != 'http' or parsed.username or parsed.password:
                raise ValueError('Invalid proxy request')
            remote = connect_public(parsed.hostname, parsed.port or 80)
            target = (parsed.path or '/') + ('?'+parsed.query if parsed.query else '')
            lines = [f'{self.command} {target} HTTP/1.0']
            for key, value in self.headers.items():
                if key.lower() not in ('connection','proxy-connection','proxy-authorization','transfer-encoding','content-length'):
                    lines.append(f'{key}: {value}')
            lines.append('Connection: close\r\n\r\n')
            remote.sendall('\r\n'.join(lines).encode('iso-8859-1'))
            while True:
                chunk = remote.recv(65536)
                if not chunk:
                    break
                self.connection.sendall(chunk)
        except (OSError, ValueError, TypeError):
            if remote is None:
                self.send_error(403, 'Destination blocked or unavailable')
        finally:
            if remote:
                remote.close()
    do_HEAD = do_GET


class ProxyServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    gate = threading.BoundedSemaphore(24)
    def process_request(self, request, client_address):
        if not self.gate.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.gate.release()
            raise
    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.gate.release()


def start_proxy():
    server = ProxyServer(('127.0.0.1', 0), Proxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f'http://127.0.0.1:{server.server_port}'
