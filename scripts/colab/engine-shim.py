#!/usr/bin/env python3
"""engine-shim：让本机 workbench 以为在本地起引擎，实际把推理转到 Colab。

为什么要它：`internal/engine` 只认四件事（见 docs/colab-offload.md），并不检查子进程是谁。
本脚本作为 `WORKBENCH_ENGINE` 被启动，满足这四条，把请求转发到远端 audiocpp_server。

    export WORKBENCH_ENGINE=/path/to/scripts/colab/engine-shim.py
    export COLAB_ENGINE_SESSION=acpp                # colab ssh -s 的会话名
    export COLAB_ENGINE_REMOTE_BIN=/content/audio.cpp/build/bin/audiocpp_server
    export COLAB_ENGINE_REMOTE_ROOT=/content/models

约定：
  * 本机模型路径按文件名映射到远端目录（models[].path 的 basename）。
  * 隧道端口在远端重新分配，本机始终监听 workbench 给的端口。
  * 父进程结束时（stdin 关闭或 SIGTERM）杀掉远端进程与隧道。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 转发时不改动这些方法，其余按需透传；目前 workbench 只用 GET 与 POST。
FORWARD_HEADERS = {"content-type", "accept", "content-length"}


def log(message: str) -> None:
    """诊断信息写 stderr：stdout 留给父进程读，混进日志会让就绪探测失效。"""
    print(f"[colab-engine] {message}", file=sys.stderr, flush=True)


def parse_args(argv: list[str]) -> Path:
    """只接受 `--config <path> [--no-ui]`；其余参数直接忽略，交给远端。"""
    config = None
    index = 0
    while index < len(argv):
        if argv[index] == "--config" and index + 1 < len(argv):
            config = Path(argv[index + 1])
            index += 2
            continue
        index += 1
    if config is None:
        raise SystemExit("用法：engine-shim.py --config <server.json> --no-ui")
    return config


def free_port() -> int:
    """在远端取空闲端口；这里在本机取，隧道两端端口一致最省事。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def remote_exec(session: str, command: str) -> list[str]:
    """通过 colab ssh 的 ProxyCommand 桥执行远端命令，返回可直接传给 subprocess 的参数。"""
    ssh = shutil.which("ssh")
    if ssh is None:
        raise SystemExit("需要 ssh；请先安装 OpenSSH 客户端")
    colab = shutil.which("colab")
    if colab is None:
        raise SystemExit("需要 colab CLI；uv tool install google-colab-cli")
    identity = os.environ.get("COLAB_ENGINE_IDENTITY", str(Path.home() / ".ssh" / "id_ed25519"))
    proxy = f"{colab} ssh --proxy-mode -s {session}"
    return [
        ssh, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ProxyCommand={proxy}", "-i", identity,
        "-o", "ServerAliveInterval=15", "colab", command,
    ]


def rewrite_config(config: dict, remote_root: str) -> dict:
    """把本机模型路径换成远端路径，其余原样；端口由调用方写入。"""
    for entry in config.get("models", []):
        local = Path(entry.get("path", ""))
        entry["path"] = f"{remote_root.rstrip('/')}/{local.name}"
    config["host"] = "127.0.0.1"
    # 远端是真正的 CUDA 机器，本机设置里的 backend 只对本机有效，这里固定为 cuda。
    config["backend"] = "cuda"
    return config


class ProxyHandler(BaseHTTPRequestHandler):
    """把请求原样转给远端引擎，再把响应原样写回。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 与基类签名一致，否则静态检查报覆盖不兼容
        log(format % args)

    def _proxy(self) -> None:
        upstream = self.server.upstream  # type: ignore[attr-defined]
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        address, port = upstream
        try:
            with socket.create_connection((address, port), timeout=600) as sock:
                head = [f"{self.command} {self.path} HTTP/1.1", f"Host: {address}:{port}"]
                for key, value in self.headers.items():
                    if key.lower() in FORWARD_HEADERS:
                        head.append(f"{key}: {value}")
                head.append(f"Content-Length: {len(body)}")
                sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + body)
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except OSError as error:
            log(f"转发 {self.command} {self.path} 失败：{error}")
            self.send_error(502, "引擎不可达")

    do_GET = do_POST = do_PUT = do_DELETE = _proxy


def wait_healthy(port: int, timeout: float) -> bool:
    """父进程只探测 /health，但隧道自身也要先通；这里等一次就绪，失败就报错退出。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def main() -> int:
    config_path = parse_args(sys.argv[1:])
    config = json.loads(config_path.read_text())
    session = os.environ.get("COLAB_ENGINE_SESSION", "acpp")
    remote_bin = os.environ.get("COLAB_ENGINE_REMOTE_BIN", "/content/audio.cpp/build/bin/audiocpp_server")
    remote_root = os.environ.get("COLAB_ENGINE_REMOTE_ROOT", "/content/models")

    local_port = int(config["port"])
    remote_port = free_port()
    config = rewrite_config(config, remote_root)
    config["port"] = remote_port

    # 远端配置文件写在本机临时目录，随命令一起送过去（base64 避免引号转义问题）。
    import base64

    payload = base64.b64encode(json.dumps(config).encode()).decode()
    remote_config = f"/content/.yovoice-server-{remote_port}.json"
    bootstrap = (
        f"echo {payload} | base64 -d > {remote_config} && "
        f"cd {os.path.dirname(remote_bin)} && "
        f"exec {remote_bin} --config {remote_config} --no-ui"
    )
    remote = subprocess.Popen(remote_exec(session, bootstrap))
    tunnel = subprocess.Popen(remote_exec(session, f"-N -L 127.0.0.1:{local_port}:127.0.0.1:{remote_port}"))

    def shutdown(*_args) -> None:
        """父进程退出时一并清理：远端进程与隧道都不该留在 Colab 上白烧算力。"""
        for process in (tunnel, remote):
            if process.poll() is None:
                process.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    if not wait_healthy(local_port, timeout=float(os.environ.get("COLAB_ENGINE_TUNNEL_TIMEOUT", "120"))):
        log("隧道未就绪，退出（检查 colab ssh 会话名与远端引擎路径）")
        shutdown()

    server = ThreadingHTTPServer(("127.0.0.1", local_port), ProxyHandler)
    server.upstream = ("127.0.0.1", remote_port)  # type: ignore[attr-defined]
    log(f"本机 {local_port} → Colab {session}:{remote_port}，引擎 {remote_bin}")

    # stdin 关闭即父进程退出，与服务进程的约定一致。
    threading.Thread(target=lambda: (sys.stdin.read(), shutdown()), daemon=True).start()
    try:
        server.serve_forever()
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
