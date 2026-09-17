"""Small local HTTP proxy that chains through a local front proxy first.

Clients (Playwright, curl_cffi, httpx) can only receive one proxy URL.  This
module exposes that one URL and internally performs:

    client -> 127.0.0.1:chain_port -> local_proxy -> exit_proxy -> target

If no exit proxies are configured, it simply forwards through local_proxy.
"""
from __future__ import annotations

import base64
import json
import os
import random
import select
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

from proxy_pool import (
    live_proxy_pool,
    normalize_proxy_strategy,
    parse_proxy_pool,
    record_proxy_result,
)


DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 17997
CONNECT_TIMEOUT = max(1.0, min(30.0, float(os.getenv("PROGROK_CHAIN_CONNECT_TIMEOUT", "6") or 6)))
MAX_EXIT_TRIES = max(0, min(20, int(os.getenv("PROGROK_CHAIN_MAX_EXIT_TRIES", "2") or 2)))
MIHOMO_RANDOMIZE = str(os.getenv("PROGROK_MIHOMO_RANDOMIZE", "1")).lower() not in {"0", "false", "no", "off"}
MIHOMO_URL = os.getenv("PROGROK_MIHOMO_URL", "http://127.0.0.1:9097").rstrip("/")
MIHOMO_GROUP = os.getenv("PROGROK_MIHOMO_GROUP", "Proxy")
MIHOMO_PROXY_PORT = int(os.getenv("PROGROK_MIHOMO_PROXY_PORT", "7897") or 7897)
MIHOMO_SECRET_FILE = os.getenv("PROGROK_MIHOMO_SECRET_FILE", "/etc/progrok/mihomo-secret")
MIHOMO_CACHE_TTL = max(1.0, min(300.0, float(os.getenv("PROGROK_MIHOMO_CACHE_TTL", "30") or 30)))
_mihomo_lock = threading.RLock()
_mihomo_nodes: list[str] = []
_mihomo_nodes_at = 0.0
_mihomo_last_node = ""


class _State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.local_proxies: list[str] = []
        self.exit_proxies: list[str] = []
        self.strategy = "round_robin"
        self.rr = 0

    def configure(self, *, local_proxy: str, exit_proxies: Iterable[str], strategy: str) -> None:
        with self.lock:
            local_pool = parse_proxy_pool(str(local_proxy or ""), fallback_env=False)
            if not local_pool:
                local_pool = ["http://127.0.0.1:7897"]
            self.local_proxies = [str(p).strip() for p in local_pool if str(p).strip()]
            self.exit_proxies = [str(p).strip() for p in exit_proxies if str(p).strip()]
            self.strategy = normalize_proxy_strategy(strategy)

    def pick_local(self) -> str:
        with self.lock:
            pool = live_proxy_pool(self.local_proxies)
            if not pool:
                return ""
            if len(pool) == 1:
                return pool[0]
            return random.choice(pool)

    def candidate_exits(self) -> list[str]:
        with self.lock:
            pool = live_proxy_pool(self.exit_proxies)
            if not pool:
                return []
            if len(pool) == 1:
                return list(pool)
            shuffled = list(pool)
            random.shuffle(shuffled)
            return shuffled

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "local_proxy": self.local_proxies[0] if self.local_proxies else "",
                "local_count": len(self.local_proxies),
                "exit_count": len(self.exit_proxies),
                "strategy": self.strategy,
            }


_state = _State()
_server_lock = threading.RLock()
_server: ThreadingHTTPServer | None = None
_server_thread: threading.Thread | None = None
_server_port = 0


def _proxy_auth_header(username: str | None, password: str | None) -> str:
    user = unquote(username or "")
    if not user:
        return ""
    raw = f"{user}:{unquote(password or '')}".encode("utf-8")
    return "Proxy-Authorization: Basic " + base64.b64encode(raw).decode("ascii") + "\r\n"


def _host_port_from_proxy(proxy_url: str) -> tuple[str, int, str, str, str]:
    parsed = urlparse(proxy_url)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"}:
        raise OSError(f"unsupported proxy scheme: {parsed.scheme}")
    if not parsed.hostname or not parsed.port:
        raise OSError("proxy must include host and port")
    return parsed.hostname, int(parsed.port), parsed.scheme, unquote(parsed.username or ""), unquote(parsed.password or "")


def _is_mihomo_entry(proxy_url: str) -> bool:
    try:
        parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
        host = (parsed.hostname or "").lower()
        return host in {"127.0.0.1", "localhost", "::1"} and int(parsed.port or 0) == MIHOMO_PROXY_PORT
    except Exception:
        return False


def _mihomo_secret() -> str:
    try:
        with open(MIHOMO_SECRET_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def _mihomo_api(method: str, path: str, body: dict[str, object] | None = None) -> object:
    headers: dict[str, str] = {}
    secret = _mihomo_secret()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(MIHOMO_URL + path, data=data, headers=headers, method=method)
    with urlopen(req, timeout=2.5) as resp:
        raw = resp.read()
        return json.loads(raw.decode("utf-8")) if raw else {}


def _mihomo_group_nodes() -> list[str]:
    global _mihomo_nodes, _mihomo_nodes_at
    now = time.time()
    with _mihomo_lock:
        if _mihomo_nodes and now - _mihomo_nodes_at <= MIHOMO_CACHE_TTL:
            return list(_mihomo_nodes)
        data = _mihomo_api("GET", "/proxies")
        proxies = data.get("proxies", {}) if isinstance(data, dict) else {}
        group = proxies.get(MIHOMO_GROUP, {}) if isinstance(proxies, dict) else {}
        nodes = [
            str(item)
            for item in (group.get("all") or [])
            if str(item) and str(item).upper() not in {"DIRECT", "REJECT"}
        ]
        _mihomo_nodes = nodes
        _mihomo_nodes_at = now
        return list(nodes)


def _randomize_mihomo_if_needed(proxy_url: str) -> None:
    global _mihomo_last_node
    if not MIHOMO_RANDOMIZE or not _is_mihomo_entry(proxy_url):
        return
    try:
        nodes = _mihomo_group_nodes()
        if not nodes:
            return
        candidates = [node for node in nodes if node != _mihomo_last_node] or nodes
        node = random.choice(candidates)
        qgroup = quote(MIHOMO_GROUP, safe="")
        _mihomo_api("PUT", f"/proxies/{qgroup}", {"name": node})
        _mihomo_last_node = node
    except Exception:
        # Random switching is an optimization; never fail the registration path.
        return


def _read_headers(sock: socket.socket, *, timeout: float = CONNECT_TIMEOUT) -> bytes:
    sock.settimeout(timeout)
    data = bytearray()
    while b"\r\n\r\n" not in data and len(data) < 65536:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def _require_http_200(header_block: bytes, *, hop: str) -> None:
    first = header_block.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
    parts = first.split()
    if len(parts) < 2 or parts[1] != "200":
        raise OSError(f"{hop} CONNECT failed: {first[:160]}")


def _connect_socks5(sock: socket.socket, host: str, port: int, username: str = "", password: str = "") -> None:
    if username:
        sock.sendall(b"\x05\x02\x00\x02")
    else:
        sock.sendall(b"\x05\x01\x00")
    resp = sock.recv(2)
    if len(resp) != 2 or resp[0] != 5:
        raise OSError("SOCKS5 greeting failed")
    method = resp[1]
    if method == 2:
        u = username.encode("utf-8")
        p = password.encode("utf-8")
        if len(u) > 255 or len(p) > 255:
            raise OSError("SOCKS5 credentials are too long")
        sock.sendall(bytes([1, len(u)]) + u + bytes([len(p)]) + p)
        auth = sock.recv(2)
        if len(auth) != 2 or auth[1] != 0:
            raise OSError("SOCKS5 auth failed")
    elif method != 0:
        raise OSError("SOCKS5 proxy rejected auth methods")
    h = host.encode("idna")
    if len(h) > 255:
        raise OSError("SOCKS5 hostname is too long")
    req = b"\x05\x01\x00\x03" + bytes([len(h)]) + h + int(port).to_bytes(2, "big")
    sock.sendall(req)
    head = sock.recv(4)
    if len(head) != 4 or head[1] != 0:
        raise OSError(f"SOCKS5 connect failed: {head.hex()}")
    atyp = head[3]
    if atyp == 1:
        extra = 4
    elif atyp == 3:
        extra = sock.recv(1)[0]
    elif atyp == 4:
        extra = 16
    else:
        raise OSError("SOCKS5 invalid address type")
    if extra:
        sock.recv(extra)
    sock.recv(2)


def _open_socket_to(host: str, port: int, *, via_proxy: str = "", timeout: float = CONNECT_TIMEOUT) -> socket.socket:
    front = (via_proxy or "").strip()
    if not front:
        return socket.create_connection((host, int(port)), timeout=timeout)

    fhost, fport, fscheme, fuser, fpass = _host_port_from_proxy(front)
    _randomize_mihomo_if_needed(front)
    sock = socket.create_connection((fhost, fport), timeout=timeout)
    if fscheme == "https":
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=fhost)
    if fscheme in {"socks5", "socks5h"}:
        _connect_socks5(sock, host, int(port), fuser, fpass)
        return sock

    connect = (
        f"CONNECT {host}:{int(port)} HTTP/1.1\r\n"
        f"Host: {host}:{int(port)}\r\n"
        f"{_proxy_auth_header(fuser, fpass)}"
        "Proxy-Connection: keep-alive\r\n"
        "\r\n"
    ).encode("latin1")
    sock.sendall(connect)
    _require_http_200(_read_headers(sock), hop="front proxy")
    return sock


def _open_target_socket(target_host: str, target_port: int, *, exit_proxy: str = "") -> socket.socket:
    local_proxy = str(_state.pick_local() or "")
    exit_url = str(exit_proxy or "").strip()
    if not exit_url:
        return _open_socket_to(target_host, target_port, via_proxy=local_proxy)

    ehost, eport, escheme, euser, epass = _host_port_from_proxy(exit_url)
    sock = _open_socket_to(ehost, eport, via_proxy=local_proxy)
    if escheme == "https":
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=ehost)
    if escheme in {"socks5", "socks5h"}:
        _connect_socks5(sock, target_host, int(target_port), euser, epass)
        return sock

    connect = (
        f"CONNECT {target_host}:{int(target_port)} HTTP/1.1\r\n"
        f"Host: {target_host}:{int(target_port)}\r\n"
        f"{_proxy_auth_header(euser, epass)}"
        "Proxy-Connection: keep-alive\r\n"
        "\r\n"
    ).encode("latin1")
    sock.sendall(connect)
    _require_http_200(_read_headers(sock), hop="exit proxy")
    return sock


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 300

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _client_allowed(self) -> bool:
        host = str(self.client_address[0] if self.client_address else "")
        return (
            host == "::1"
            or host.startswith("127.")
            or host.startswith("10.")
            or host.startswith("192.168.")
            or any(host.startswith(f"172.{i}.") for i in range(16, 32))
        )

    def handle_one_request(self) -> None:
        if not self._client_allowed():
            try:
                self.send_error(403, "local chain proxy only accepts local/docker clients")
            except Exception:
                pass
            return
        return super().handle_one_request()

    def do_CONNECT(self) -> None:  # noqa: N802
        host, sep, port_text = self.path.rpartition(":")
        if not sep or not host:
            self.send_error(400, "CONNECT target must be host:port")
            return
        try:
            port = int(port_text)
        except ValueError:
            self.send_error(400, "invalid CONNECT port")
            return

        upstream = self._connect_with_failover(host, port)
        if upstream is None:
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        self._pipe(upstream)

    def do_GET(self) -> None:  # noqa: N802
        self._forward_plain()

    def do_POST(self) -> None:  # noqa: N802
        self._forward_plain()

    def do_HEAD(self) -> None:  # noqa: N802
        self._forward_plain()

    def do_PUT(self) -> None:  # noqa: N802
        self._forward_plain()

    def do_DELETE(self) -> None:  # noqa: N802
        self._forward_plain()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._forward_plain()

    def _connect_with_failover(self, host: str, port: int) -> socket.socket | None:
        exits = _state.candidate_exits()
        candidates = list(exits[:MAX_EXIT_TRIES] if MAX_EXIT_TRIES else exits)
        if _state.snapshot().get("local_proxy") and exits:
            candidates.append("")
        if not candidates:
            candidates = [""]
        last_error = ""
        for exit_proxy in candidates:
            try:
                upstream = _open_target_socket(host, port, exit_proxy=exit_proxy)
                if exit_proxy:
                    record_proxy_result(exit_proxy, ok=True)
                return upstream
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if exit_proxy:
                    record_proxy_result(exit_proxy, ok=False, error=last_error)
                continue
        self.send_error(502, f"proxy chain failed: {last_error[:180]}")
        return None

    def _forward_plain(self) -> None:
        parsed = urlparse(self.path)
        host = parsed.hostname or self.headers.get("Host", "").split(":", 1)[0]
        if not host:
            self.send_error(400, "missing Host")
            return
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        upstream = self._connect_with_failover(host, int(port))
        if upstream is None:
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        headers = []
        for key, value in self.headers.items():
            if key.lower() in {"proxy-connection", "connection"}:
                continue
            headers.append(f"{key}: {value}\r\n")
        req = (
            f"{self.command} {path} {self.request_version}\r\n"
            + "".join(headers)
            + "Connection: close\r\n\r\n"
        ).encode("latin1", errors="replace") + body
        try:
            upstream.sendall(req)
            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                self.connection.sendall(chunk)
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    def _pipe(self, upstream: socket.socket) -> None:
        sockets = [self.connection, upstream]
        try:
            for sock in sockets:
                sock.setblocking(False)
            while True:
                readable, _, exceptional = select.select(sockets, [], sockets, 300)
                if exceptional or not readable:
                    break
                for sock in readable:
                    other = upstream if sock is self.connection else self.connection
                    try:
                        data = sock.recv(65536)
                    except BlockingIOError:
                        continue
                    except (ConnectionResetError, BrokenPipeError, OSError):
                        return
                    if not data:
                        return
                    try:
                        other.sendall(data)
                    except (ConnectionResetError, BrokenPipeError, OSError):
                        return
        finally:
            try:
                upstream.close()
            except Exception:
                pass


def ensure_chain_proxy(
    *,
    local_proxy: str,
    exit_proxy_pool: str,
    proxy_username: str = "",
    proxy_password: str = "",
    proxy_strategy: str = "round_robin",
    listen_host: str = DEFAULT_LISTEN_HOST,
    listen_port: int = DEFAULT_LISTEN_PORT,
) -> dict[str, object]:
    exits = parse_proxy_pool(
        exit_proxy_pool,
        username=proxy_username or None,
        password=proxy_password or None,
        fallback_env=False,
    )
    _state.configure(local_proxy=local_proxy, exit_proxies=exits, strategy=proxy_strategy)

    global _server, _server_thread, _server_port
    with _server_lock:
        if _server is not None and _server_port != int(listen_port):
            _server.shutdown()
            _server.server_close()
            _server = None
        if _server is None:
            _server = ThreadingHTTPServer((listen_host, int(listen_port)), _Handler)
            _server_thread = threading.Thread(
                target=_server.serve_forever,
                daemon=True,
                name=f"progrok-chain-proxy-{listen_port}",
            )
            _server_thread.start()
            _server_port = int(listen_port)
    return status()


def status() -> dict[str, object]:
    snap = _state.snapshot()
    return {
        "ok": _server is not None,
        "url": f"http://127.0.0.1:{_server_port or DEFAULT_LISTEN_PORT}",
        **snap,
    }
