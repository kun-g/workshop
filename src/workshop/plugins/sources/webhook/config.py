"""推送入口的配置（核心 R37）：规则里不写就不起入口、不开端口；只许本机 loopback；secret 只认部署目录里的文件。"""
import ipaddress
from pathlib import Path

LOOPBACK_NAMES = ("localhost",)


def loopback(host):
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def settings(target):
    """交付对象的 webhook 配置 → 入口和来源共用的设置；无效时抛 ValueError，不碰网络和磁盘。"""
    config = target.get("webhook")
    if not isinstance(config, dict):
        raise ValueError(f"{target.get('name')} 没有 webhook 配置：推送来源默认关闭")
    if "secret" in config:
        raise ValueError("webhook 的 secret 不写进规则；放部署目录的文件里，用 secret_file 指过去")
    host = config.get("listen", "127.0.0.1")
    if not isinstance(host, str) or not loopback(host):
        raise ValueError(f"webhook 只许监听本机 loopback（127.0.0.1 或 ::1），拒开对外端口：{host!r}")
    port = config.get("port")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("webhook.port 须为 0 到 65535 的整数（0 由系统挑空闲端口）")
    secret = config.get("secret_file")
    if not isinstance(secret, str) or not secret:
        raise ValueError("webhook.secret_file 必填：验签 secret 所在的部署目录文件")
    spool = config.get("spool", f"~/.local/state/workshop/webhook/{target['name']}")
    if not isinstance(spool, str) or not spool:
        raise ValueError("webhook.spool 须为目录路径")
    return {"target": target["name"], "repo": target["repo"], "host": host, "port": port,
            "secret_file": str(Path(secret).expanduser()), "spool": str(Path(spool).expanduser())}
