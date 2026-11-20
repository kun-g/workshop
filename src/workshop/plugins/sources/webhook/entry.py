"""推送入口（核心 R37 判据一）：验签、原子写本地 spool、回 2xx，不在请求里等守护进程、worker 或模型。

只依赖 spool 和标准库，不打开事实表；守护进程停着也照样落盘，它起来后补消费。
叫醒靠守护进程盯着 spool 目录（来源的 wake_paths），入口不用再发信。

python -m workshop.plugins.sources.webhook.entry --config '<settings JSON>'（由 `workshop webhook --target` 起）
"""
import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
from datetime import datetime, timezone

from . import spool
from .config import loopback

LIMIT = 25 * 1024 * 1024  # GitHub 单次投递上限


def secret(path):
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, settings, log):
        self.settings, self.log = settings, log
        if ":" in settings["host"]:
            self.address_family = socket.AF_INET6
        super().__init__((settings["host"], settings["port"]), Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "workshop-webhook"
    sys_version = ""

    def log_message(self, format, *args):
        return  # 默认日志带请求行和头部；只记下面自己写的一行。

    def answer(self, status, text):
        body = (text + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        event = self.headers.get("X-GitHub-Event", "-")
        delivery = self.headers.get("X-GitHub-Delivery", "-")
        print(f"{datetime.now(timezone.utc).isoformat()} {status} {event} {delivery} {text}",
              file=self.server.log, flush=True)

    def do_POST(self):
        settings = self.server.settings
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.answer(411, "缺 Content-Length")
        if not 0 <= length <= LIMIT:
            return self.answer(413, "投递太大")
        body = self.rfile.read(length)
        key = secret(settings["secret_file"])
        if key is None:
            return self.answer(403, "没有配置验签 secret，拒收")
        claimed = self.headers.get("X-Hub-Signature-256", "")
        if not claimed:
            return self.answer(401, "缺签名，拒收")
        expected = "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(claimed, expected):
            return self.answer(401, "签名不对，拒收")
        event = self.headers.get("X-GitHub-Event", "")
        try:
            payload = json.loads(body)
        except ValueError:
            return self.answer(400, "正文不是 JSON")
        if not event or not isinstance(payload, dict):
            return self.answer(400, "缺事件类型或正文不是对象")
        spool.write(settings["spool"], {
            "event": event, "delivery": self.headers.get("X-GitHub-Delivery", ""),
            "received_at": datetime.now(timezone.utc).isoformat(), "payload": payload})
        return self.answer(202, "已落盘")

    def do_GET(self):
        return self.answer(405, "只收 POST")


def serve(settings, log=None):
    """绑好端口、还没开始收；调用方 serve_forever。非 loopback 一律拒开。"""
    if not loopback(settings["host"]):
        raise ValueError(f"webhook 只许监听本机 loopback，拒开对外端口：{settings['host']!r}")
    Path(settings["spool"]).mkdir(parents=True, exist_ok=True)
    return Server(settings, log or sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="workshop-webhook")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    server = serve(json.loads(args.config))
    host, port = server.server_address[:2]
    print(f"推送入口在 {host}:{port} 收投递，落盘到 {server.settings['spool']}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
