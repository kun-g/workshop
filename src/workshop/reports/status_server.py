"""本机只读的状态页服务：只监听 127.0.0.1，只提供 status.html 这一页，让飞书卡片上的按钮在这台电脑上打得开。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import urlsplit

PATHS = ('/', '/status.html')


def handler(page):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if urlsplit(self.path).path not in PATHS:
                self.send_error(404)
                return
            try:
                body = page.read_bytes()
            except OSError:
                self.send_error(503, 'status page not ready')  # 原因行只能是 latin-1
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    return Handler


class StatusServer:
    def __init__(self, page, url):
        parts = urlsplit(url)
        if parts.scheme != 'http' or parts.hostname != '127.0.0.1' or parts.port is None:
            raise ValueError('状态页地址必须是 http://127.0.0.1:端口/…，只在本机提供')
        self.httpd = ThreadingHTTPServer(('127.0.0.1', parts.port), handler(Path(page)))
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, name='workshop-status-server', daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
