"""Proxy pool helpers for protocol registration / outbound HTTP.

Supports multi-line proxy lists (one proxy per line) with shared optional
username/password, plus simple rotation strategies for batch jobs.

Accepted line formats:
  - http://host:port
  - http://user:pass@host:port
  - socks5://host:port
  - host:port
  - host:port:user:pass
  - scheme://host:port:user:pass  (common residential-provider style)

Legacy single-proxy config continues to work unchanged.
"""

from __future__ import annotations

import base64
import json
import os
import random
import threading
import time
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlparse, urlunparse
from urllib.request import Request, urlopen

_lock = threading.Lock()
_rr_index = 0
_health_lock = threading.Lock()
_proxy_failures: dict[str, int] = {}
_proxy_health_state: dict[str, dict[str, Any]] = {}
_proxy_health_state_file = os.getenv("PROGROK_PROXY_HEALTH_STATE_FILE", "").strip()
_proxy_dead_after = 3
_proxy_cooldown_sec = max(0, int(os.getenv("PROGROK_PROXY_COOLDOWN_SEC", "900") or 900))
_subscription_timeout = max(3.0, min(30.0, float(os.getenv("PROGROK_PROXY_SUBSCRIPTION_TIMEOUT", "15") or 15)))
_subscription_max_bytes = max(4096, min(4 * 1024 * 1024, int(os.getenv("PROGROK_PROXY_SUBSCRIPTION_MAX_BYTES", "1048576") or 1048576)))


def _env_proxy_text() -> str:
    # Prefer dedicated pool env, then the classic single-proxy vars.
    for key in (
        "GROK2API_XAI_PROXY_POOL",
        "GROK2API_PROXY_POOL",
        "GROK2API_XAI_PROXY",
        "GROK2API_PROXY",
        "GROK_CLI_PROXY",
    ):
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return ""


def _env_proxy_user() -> str:
    return (
        os.getenv("GROK2API_XAI_PROXY_USERNAME")
        or os.getenv("GROK2API_PROXY_USERNAME")
        or ""
    ).strip()


def _env_proxy_pass() -> str:
    return (
        os.getenv("GROK2API_XAI_PROXY_PASSWORD")
        or os.getenv("GROK2API_PROXY_PASSWORD")
        or ""
    ).strip()


def _looks_like_subscription_url(raw: str) -> bool:
    s = _normalize_line_scheme(raw)
    if not s or "://" not in s:
        return False
    try:
        parsed = urlparse(s)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    return bool((parsed.path or "") not in {"", "/"} or parsed.query or parsed.fragment)


def _decode_subscription_body(text: str) -> str:
    compact = "".join((text or "").split())
    if not compact:
        return ""
    padded = compact + "=" * (-len(compact) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(padded)
        except Exception:
            continue
        decoded = raw.decode("utf-8", errors="ignore").strip()
        if decoded and (" ://" in decoded or "://" in decoded or "\n" in decoded or "\r" in decoded):
            return decoded
    return ""


def _fetch_subscription_text(url: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "*/*",
        },
        method="GET",
    )
    with urlopen(req, timeout=_subscription_timeout) as resp:
        raw = resp.read(_subscription_max_bytes + 1)
    if len(raw) > _subscription_max_bytes:
        raise ValueError("subscription body too large")
    text = raw.decode("utf-8", errors="ignore").strip()
    if not text:
        return ""
    if "://" in text or "\n" in text or "\r" in text:
        return text
    decoded = _decode_subscription_body(text)
    return decoded or text


def split_proxy_text(text: str | None) -> list[str]:
    """Split multi-proxy text into raw lines (comma / newline / semicolon)."""
    raw = (text or "").strip()
    if not raw:
        return []
    # Normalize common separators while preserving URL schemes (://).
    # First split on newlines / semicolons; then on commas only when the token
    # does not look like a single URL with query string.
    chunks: list[str] = []
    for part in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        part = part.strip()
        if not part:
            continue
        if ";" in part:
            for sub in part.split(";"):
                sub = sub.strip()
                if sub:
                    chunks.append(sub)
            continue
        # Comma-separated lists: "a,b,c" — but not "http://x?a=1,b=2" (rare).
        if "," in part and "://" not in part.split(",", 1)[0]:
            for sub in part.split(","):
                sub = sub.strip()
                if sub:
                    chunks.append(sub)
            continue
        # Also allow "url1,url2" when each segment has a scheme.
        if "," in part:
            maybe = [s.strip() for s in part.split(",") if s.strip()]
            if maybe and all("://" in s or s.count(":") >= 1 for s in maybe):
                chunks.extend(maybe)
                continue
        chunks.append(part)
    # Drop comments / empty.
    out: list[str] = []
    seen: set[str] = set()
    for c in chunks:
        line = c.strip()
        if not line or line.startswith("#"):
            continue
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def _normalize_line_scheme(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    lower = s.lower()
    if lower.startswith("soket5://"):
        return "socks5://" + s.split("://", 1)[1]
    if lower.startswith("socket5://"):
        return "socks5://" + s.split("://", 1)[1]
    return s


def _hostport_userpass(raw: str) -> str | None:
    """Parse host:port:user:pass (or scheme://host:port:user:pass) → URL."""
    s = _normalize_line_scheme(raw)
    if not s:
        return None
    scheme = "http"
    rest = s
    if "://" in s:
        scheme, rest = s.split("://", 1)
        scheme = (scheme or "http").strip().lower() or "http"
        if scheme in {"soket5", "socket5"}:
            scheme = "socks5"
    # Already a normal URL with optional userinfo.
    if "@" in rest or rest.count(":") <= 1:
        if "://" not in s:
            return f"{scheme}://{rest}"
        return f"{scheme}://{rest}" if not s.startswith(f"{scheme}://") else s

    # host:port:user:pass  (user/pass may contain ':')
    # IPv6 is not supported in this shorthand (use full URL).
    parts = rest.split(":")
    if len(parts) < 4:
        if "://" not in s:
            return f"{scheme}://{rest}"
        return s
    host = parts[0].strip()
    port = parts[1].strip()
    user = parts[2]
    password = ":".join(parts[3:])
    if not host or not port:
        return None
    try:
        int(port)
    except ValueError:
        return None
    auth = quote(user, safe="")
    if password != "":
        auth = f"{auth}:{quote(password, safe='')}"
    return f"{scheme}://{auth}@{host}:{port}"


def canonicalize_proxy_line(
    raw: str,
    *,
    username: str | None = None,
    password: str | None = None,
) -> str:
    """Return a single proxy URL with optional shared auth applied.

    Raises ValueError when the line is not a usable proxy.
    """
    line = (raw or "").strip()
    if not line:
        raise ValueError("empty proxy line")
    # Expand host:port:user:pass shorthand first.
    expanded = _hostport_userpass(line) or line
    try:
        from moemail import normalize_proxy_config

        cfg = normalize_proxy_config(
            expanded,
            username=username,
            password=password,
        )
        if not cfg or not cfg.get("proxy"):
            raise ValueError("invalid proxy")
        return str(cfg["proxy"])
    except ImportError:
        pass

    parsed = urlparse(expanded if "://" in expanded else f"http://{expanded}")
    scheme = (parsed.scheme or "http").lower()
    if scheme not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError("proxy scheme must be http, https, socks5, or socks5h")
    if not parsed.hostname:
        raise ValueError("proxy must include host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("proxy port is invalid") from exc
    if not port:
        raise ValueError("proxy must include port")

    user = (username if username is not None else "").strip()
    pwd = (password if password is not None else "").strip()
    if not user and parsed.username:
        user = unquote(parsed.username)
    if not pwd and parsed.password:
        pwd = unquote(parsed.password)
    if pwd and not user:
        raise ValueError("proxy username is required when proxy password is set")

    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}"
    if user:
        auth = quote(user, safe="")
        if pwd:
            auth = f"{auth}:{quote(pwd, safe='')}"
        netloc = f"{auth}@{netloc}"
    return urlunparse((scheme, netloc, parsed.path or "", "", parsed.query or "", ""))


def parse_proxy_pool(
    text: str | None = None,
    *,
    username: str | None = None,
    password: str | None = None,
    fallback_env: bool = True,
    _depth: int = 0,
) -> list[str]:
    """Parse proxy pool text into a de-duplicated list of full proxy URLs.

    Invalid lines are skipped (not raised) so a large paste still yields the
    usable subset. Callers that need strict validation should use
    ``validate_proxy_pool``.
    """
    raw = (text if text is not None else "").strip()
    if not raw and fallback_env:
        raw = _env_proxy_text()
    lines = split_proxy_text(raw)
    if not lines:
        return []

    user = username
    pwd = password
    if user is None and fallback_env:
        user = _env_proxy_user() or None
    if pwd is None and fallback_env:
        pwd = _env_proxy_pass() or None
    # Empty string means "explicitly none"; None means "use default/env".
    user_s = None if user is None else str(user).strip()
    pass_s = None if pwd is None else str(pwd).strip()

    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if _looks_like_subscription_url(line):
            if _depth >= 2:
                continue
            try:
                nested_text = _fetch_subscription_text(line)
            except Exception:
                continue
            nested = parse_proxy_pool(
                nested_text,
                username=user_s,
                password=pass_s,
                fallback_env=False,
                _depth=_depth + 1,
            )
            for url in nested:
                if url and url not in seen:
                    seen.add(url)
                    out.append(url)
            continue
        try:
            url = canonicalize_proxy_line(line, username=user_s, password=pass_s)
        except Exception:
            continue
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def validate_proxy_pool(
    text: str | None = None,
    *,
    username: str | None = None,
    password: str | None = None,
    fallback_env: bool = False,
) -> dict[str, Any]:
    """Validate every non-empty line; return ok/errors/proxies summary."""
    lines = split_proxy_text(text or "")
    if not lines and fallback_env:
        lines = split_proxy_text(_env_proxy_text())
    user = username
    pwd = password
    if user is None and fallback_env:
        user = _env_proxy_user() or None
    if pwd is None and fallback_env:
        pwd = _env_proxy_pass() or None
    user_s = None if user is None else str(user).strip()
    pass_s = None if pwd is None else str(pwd).strip()

    proxies: list[str] = []
    errors: list[dict[str, str]] = []
    for i, line in enumerate(lines, start=1):
        try:
            url = canonicalize_proxy_line(line, username=user_s, password=pass_s)
            proxies.append(url)
        except Exception as e:  # noqa: BLE001
            errors.append({"line": i, "raw": line[:200], "error": str(e)[:200]})
    return {
        "ok": not errors and bool(proxies),
        "count": len(proxies),
        "proxies": proxies,
        "errors": errors,
        "empty": not lines,
    }


def normalize_proxy_strategy(value: str | None) -> str:
    s = (value or "round_robin").strip().lower().replace("-", "_")
    if s in {"rr", "round", "roundrobin", "round_robin"}:
        return "round_robin"
    if s in {"rand", "random"}:
        return "random"
    if s in {"sticky", "first", "fixed"}:
        return "sticky"
    return "round_robin"


def pick_proxy(
    proxies: Iterable[str] | None,
    *,
    strategy: str | None = "round_robin",
    index: int | None = None,
) -> str | None:
    """Pick one proxy URL from a pool.

    - round_robin: global counter (thread-safe), or ``index`` when provided
    - random: uniform random
    - sticky: always first
    """
    pool = [str(p).strip() for p in (proxies or []) if str(p).strip()]
    if not pool:
        return None
    mode = normalize_proxy_strategy(strategy)
    if mode == "sticky" or len(pool) == 1:
        return pool[0]
    if mode == "random":
        return random.choice(pool)
    # round_robin
    if index is not None:
        return pool[int(index) % len(pool)]
    global _rr_index
    with _lock:
        i = _rr_index
        _rr_index = (i + 1) % (10**9)
    return pool[i % len(pool)]


def reset_proxy_health() -> None:
    """Clear in-memory proxy failure counters."""
    with _health_lock:
        _proxy_failures.clear()
        _proxy_health_state.clear()


def _blank_proxy_state() -> dict[str, Any]:
    return {
        "fail_count": 0,
        "success_count": 0,
        "disabled": False,
        "cooldown_until": 0.0,
        "last_error": "",
        "last_success_at": 0.0,
        "last_failure_at": 0.0,
        "last_probe_at": 0.0,
        "probe_status": "unknown",
        "latency_ms": 0,
        "exit_ip": "",
    }


def _normalize_proxy_state(value: Any) -> dict[str, Any]:
    state = _blank_proxy_state()
    if isinstance(value, dict):
        state.update({k: v for k, v in value.items() if k in state})
    try:
        state["fail_count"] = max(0, int(state.get("fail_count") or 0))
    except (TypeError, ValueError):
        state["fail_count"] = 0
    try:
        state["success_count"] = max(0, int(state.get("success_count") or 0))
    except (TypeError, ValueError):
        state["success_count"] = 0
    state["disabled"] = bool(state.get("disabled"))
    try:
        state["cooldown_until"] = float(state.get("cooldown_until") or 0.0)
    except (TypeError, ValueError):
        state["cooldown_until"] = 0.0
    for key in ("last_success_at", "last_failure_at", "last_probe_at"):
        try:
            state[key] = float(state.get(key) or 0.0)
        except (TypeError, ValueError):
            state[key] = 0.0
    try:
        state["latency_ms"] = max(0, int(state.get("latency_ms") or 0))
    except (TypeError, ValueError):
        state["latency_ms"] = 0
    state["last_error"] = str(state.get("last_error") or "")
    state["probe_status"] = str(state.get("probe_status") or "unknown")
    state["exit_ip"] = str(state.get("exit_ip") or "")
    return state


def load_proxy_health_state(path: str | os.PathLike[str] | None) -> dict[str, dict[str, Any]]:
    """Load durable proxy health state. Invalid files are treated as empty."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(proxy).strip(): _normalize_proxy_state(value)
        for proxy, value in raw.items()
        if str(proxy).strip()
    }


def save_proxy_health_state(
    path: str | os.PathLike[str] | None,
    state: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Persist proxy health state atomically."""
    if not path:
        return
    data = state if state is not None else _proxy_health_state
    target = os.fspath(path)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{target}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data or {}, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, target)


def configure_proxy_health(
    *,
    dead_after: int | str | None = None,
    cooldown_sec: int | str | None = None,
    state_file: str | os.PathLike[str] | None = None,
) -> None:
    """Apply runtime proxy health settings from UI/config/env."""
    global _proxy_dead_after, _proxy_cooldown_sec, _proxy_health_state_file
    with _health_lock:
        if dead_after is not None:
            try:
                _proxy_dead_after = max(1, int(dead_after))
            except (TypeError, ValueError):
                pass
        if cooldown_sec is not None:
            try:
                _proxy_cooldown_sec = max(0, int(cooldown_sec))
            except (TypeError, ValueError):
                pass
        if state_file is not None:
            _proxy_health_state_file = os.fspath(state_file)
            _proxy_health_state.clear()
            _proxy_health_state.update(load_proxy_health_state(_proxy_health_state_file))


def _state_for_proxy(
    proxy: str,
    state: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    store = _proxy_health_state if state is None else state
    current = _normalize_proxy_state(store.get(proxy))
    store[proxy] = current
    return current


if _proxy_health_state_file:
    _proxy_health_state.update(load_proxy_health_state(_proxy_health_state_file))


def _is_proxy_failure_error(error: str | None) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    terms = (
        "proxy",
        "node",
        "节点",
        "代理",
        "connect",
        "connection",
        "timed out",
        "timeout",
        "tunnel",
        "econn",
        "enet",
        "net::",
        "err_proxy",
        "service unavailable",
        "http error 503",
        "503",
        "socks",
        "tls handshake",
        "no route",
        "network is unreachable",
    )
    return any(term in text for term in terms)


def record_proxy_result(
    proxy_url: str | None,
    *,
    ok: bool,
    error: str | None = None,
    dead_after: int | None = None,
    state: dict[str, dict[str, Any]] | None = None,
    max_failures: int | None = None,
    cooldown_sec: int | None = None,
    persist: bool = True,
) -> None:
    """Record one registration attempt result for a proxy.

    Successful use resets the counter. Failed business logic is ignored; only
    proxy/network-looking errors burn a proxy slot.
    """
    proxy = str(proxy_url or "").strip()
    if not proxy:
        return
    limit = int(max_failures or dead_after or _proxy_dead_after)
    cooldown = _proxy_cooldown_sec if cooldown_sec is None else max(0, int(cooldown_sec))
    now = time.time()
    with _health_lock:
        item = _state_for_proxy(proxy, state)
        if ok:
            _proxy_failures.pop(proxy, None)
            item["fail_count"] = 0
            item["success_count"] = int(item.get("success_count") or 0) + 1
            item["disabled"] = False
            item["cooldown_until"] = 0.0
            item["last_error"] = ""
            item["last_success_at"] = now
            if state is None and persist:
                save_proxy_health_state(_proxy_health_state_file)
            return
        if not _is_proxy_failure_error(error):
            return
        failures = min(limit, int(item.get("fail_count") or _proxy_failures.get(proxy, 0)) + 1)
        item["fail_count"] = failures
        item["last_error"] = str(error or "")[:1000]
        item["last_failure_at"] = now
        if failures >= limit:
            item["disabled"] = True
            item["cooldown_until"] = now + cooldown if cooldown else 0.0
        _proxy_failures[proxy] = failures
        if state is None and persist:
            save_proxy_health_state(_proxy_health_state_file)


def is_proxy_dead(proxy_url: str | None, *, dead_after: int | None = None) -> bool:
    proxy = str(proxy_url or "").strip()
    if not proxy:
        return False
    limit = int(dead_after or _proxy_dead_after)
    with _health_lock:
        item = _normalize_proxy_state(_proxy_health_state.get(proxy))
        if bool(item.get("disabled")):
            return True
        cooldown_until = float(item.get("cooldown_until") or 0.0)
        if cooldown_until and cooldown_until > time.time():
            return True
        return int(item.get("fail_count") or _proxy_failures.get(proxy, 0)) >= limit


def live_proxy_pool(
    proxies: Iterable[str] | None,
    *,
    dead_after: int | None = None,
) -> list[str]:
    pool = [str(p).strip() for p in (proxies or []) if str(p).strip()]
    return [proxy for proxy in pool if not is_proxy_dead(proxy, dead_after=dead_after)]


def pick_live_proxy(
    proxies: Iterable[str] | None,
    *,
    strategy: str | None = "round_robin",
    index: int | None = None,
    dead_after: int | None = None,
) -> str | None:
    pool = live_proxy_pool(proxies, dead_after=dead_after)
    if not pool:
        return None
    return pick_proxy(pool, strategy=strategy, index=index)


def proxy_health_summary(proxies: Iterable[str] | None = None) -> dict[str, Any]:
    pool = [str(p).strip() for p in (proxies or []) if str(p).strip()]
    with _health_lock:
        failures = dict(_proxy_failures)
        states = {k: _normalize_proxy_state(v) for k, v in _proxy_health_state.items()}
    if not pool:
        disabled = [p for p, s in states.items() if bool(s.get("disabled"))]
        return {
            "dead_after": _proxy_dead_after,
            "cooldown_sec": _proxy_cooldown_sec,
            "failures": failures,
            "state_count": len(states),
            "disabled": len(disabled),
        }
    dead = [p for p in pool if int(failures.get(p, 0)) >= _proxy_dead_after]
    now = time.time()
    disabled = [
        p
        for p in pool
        if bool(states.get(p, {}).get("disabled"))
        or float(states.get(p, {}).get("cooldown_until") or 0.0) > now
        or int(states.get(p, {}).get("fail_count") or failures.get(p, 0)) >= _proxy_dead_after
    ]
    return {
        "dead_after": _proxy_dead_after,
        "cooldown_sec": _proxy_cooldown_sec,
        "total": len(pool),
        "live": len(pool) - len(set(disabled)),
        "dead": len(set(dead) | set(disabled)),
        "disabled": len(set(disabled)),
        "failures": {p: failures[p] for p in pool if p in failures},
        "states": {
            p: {
                "fail_count": int(states[p].get("fail_count") or 0),
                "disabled": bool(states[p].get("disabled")),
                "cooldown_until": float(states[p].get("cooldown_until") or 0.0),
                "last_error": str(states[p].get("last_error") or "")[:200],
            }
            for p in pool
            if p in states
        },
    }


def resolve_proxy_for_request(
    *,
    proxy: str | None = None,
    proxy_username: str | None = None,
    proxy_password: str | None = None,
    strategy: str | None = None,
    index: int | None = None,
    fallback_env: bool = True,
) -> str | None:
    """High-level: parse pool text + pick one URL for this job/request."""
    pool = parse_proxy_pool(
        proxy,
        username=proxy_username,
        password=proxy_password,
        fallback_env=fallback_env,
    )
    if not pool:
        return None
    strat = strategy
    if strat is None:
        strat = (
            os.getenv("GROK2API_PROXY_STRATEGY")
            or os.getenv("GROK2API_XAI_PROXY_STRATEGY")
            or "round_robin"
        )
    return pick_proxy(pool, strategy=strat, index=index)


def pool_summary(
    text: str | None = None,
    *,
    username: str | None = None,
    password: str | None = None,
    strategy: str | None = None,
    fallback_env: bool = True,
) -> dict[str, Any]:
    pool = parse_proxy_pool(
        text,
        username=username,
        password=password,
        fallback_env=fallback_env,
    )
    return {
        "enabled": bool(pool),
        "count": len(pool),
        "strategy": normalize_proxy_strategy(strategy),
        # Mask credentials in previews.
        "preview": [_mask_proxy_url(p) for p in pool[:8]],
    }


def _mask_proxy_url(url: str) -> str:
    try:
        p = urlparse(url)
        if not p.hostname:
            return url[:48]
        host = p.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{p.port}" if p.port else ""
        user = unquote(p.username) if p.username else ""
        if user:
            return f"{p.scheme}://{user}:***@{host}{port}"
        return f"{p.scheme}://{host}{port}"
    except Exception:
        return (url or "")[:48]


def httpx_proxy_arg(proxy_url: str | None) -> str | None:
    """httpx Client(proxy=...) expects a single URL string (or None)."""
    s = (proxy_url or "").strip()
    return s or None


def curl_proxies_arg(proxy_url: str | None) -> dict[str, str] | None:
    """curl_cffi / requests style proxies dict."""
    s = (proxy_url or "").strip()
    if not s:
        return None
    return {"http": s, "https": s}


# ── Registration proxy chaining ────────────────────────────────────────────


def chain_proxy_url(*, port: int | str | None = None) -> str:
    """Local HTTP proxy URL used to wrap local-front + exit-pool chains."""
    try:
        p = int(port if port is not None else (os.getenv("PROGROK_CHAIN_PROXY_PORT") or 17997))
    except (TypeError, ValueError):
        p = 17997
    p = max(1, min(65535, p))
    return f"http://127.0.0.1:{p}"


def _first_proxy_url(
    text: str | None,
    *,
    username: str | None = None,
    password: str | None = None,
) -> str:
    pool = parse_proxy_pool(
        text,
        username=username,
        password=password,
        fallback_env=False,
    )
    return pool[0] if pool else ""


def _proxy_identity(url: str | None) -> tuple[str, str, int | None]:
    try:
        parsed = urlparse(str(url or "").strip())
        return (
            (parsed.scheme or "http").lower(),
            (parsed.hostname or "").lower(),
            parsed.port,
        )
    except Exception:
        return ("", "", None)


def _same_endpoint(left: str | None, right: str | None) -> bool:
    return bool(left and right) and _proxy_identity(left) == _proxy_identity(right)


def proxy_runtime_plan(
    *,
    local_proxy: str | None = None,
    exit_proxy_pool: str | None = None,
    proxy_username: str | None = None,
    proxy_password: str | None = None,
    proxy_strategy: str | None = None,
    chain_port: int | str | None = None,
    fallback_env: bool = False,
) -> dict[str, Any]:
    """Return effective runtime proxies for registration / solver.

    - local_proxy only: clients use that proxy directly.
    - exit pool only: legacy direct pool behavior.
    - local_proxy + exit pool: clients use one local chain-proxy; that chain
      proxy connects to each exit proxy through local_proxy.
    """
    default_local = (os.getenv("PROGROK_LOCAL_MIHOMO_PROXY") or "http://127.0.0.1:7897").strip() or "http://127.0.0.1:7897"
    local_text = (local_proxy if local_proxy is not None else default_local).strip() or default_local
    local_pool = parse_proxy_pool(
        local_text,
        username=proxy_username or None,
        password=proxy_password or None,
        fallback_env=fallback_env,
    )
    if not local_pool:
        # Subscription URLs such as vless/vmess Clash providers are not HTTP
        # proxies themselves.  Keep using Mihomo's mixed-port; Mihomo owns
        # provider parsing and node selection.
        if _looks_like_subscription_url(local_text):
            local_text = default_local
            local_pool = parse_proxy_pool(default_local, fallback_env=False) or [default_local]
        else:
            local_pool = [local_text]
    exits = parse_proxy_pool(
        exit_proxy_pool,
        username=proxy_username or None,
        password=proxy_password or None,
        fallback_env=fallback_env,
    )
    exits = live_proxy_pool(exits)
    if local_pool:
        exits = [
            item
            for item in exits
            if not any(_same_endpoint(item, local) for local in local_pool)
        ]
    chain_url = chain_proxy_url(port=chain_port)
    chain_enabled = bool(local_pool and exits)
    runtime = [chain_url] if chain_enabled else (local_pool or exits)
    return {
        "local_proxy": local_text,
        "local_pool": local_pool,
        "exit_pool": exits,
        "chain_enabled": chain_enabled,
        "chain_proxy_url": chain_url,
        "runtime_pool": runtime,
        "runtime_proxy": chain_url if chain_enabled else "\n".join(runtime),
        "proxy_strategy": normalize_proxy_strategy(proxy_strategy),
    }


# ── Outbound (account pool) proxy selection ─────────────────────────────────


def get_outbound_proxy_source() -> dict[str, Any]:
    """Load effective outbound proxy pool text/auth/strategy.

    Preference order:
      1) settings_store.outbound_proxy_config (admin UI)
      2) env GROK2API_XAI_PROXY_POOL / GROK2API_XAI_PROXY
      3) registration_config.proxy (shared pool fallback)
    """
    text = ""
    user = ""
    password = ""
    strategy = "round_robin"
    enabled = True
    source = "none"

    try:
        from settings_store import get_outbound_proxy_config

        cfg = get_outbound_proxy_config(include_secrets=True) or {}
        if isinstance(cfg, dict):
            enabled = bool(cfg.get("enabled", True))
            text = str(cfg.get("proxy") or "").strip()
            user = str(cfg.get("proxy_username") or "").strip()
            password = str(cfg.get("proxy_password") or "").strip()
            strategy = normalize_proxy_strategy(
                str(cfg.get("proxy_strategy") or "round_robin")
            )
            if text:
                source = "settings"
    except Exception:
        pass

    if not text:
        env_text = _env_proxy_text()
        if env_text:
            text = env_text
            user = user or _env_proxy_user()
            password = password or _env_proxy_pass()
            strategy = normalize_proxy_strategy(
                os.getenv("GROK2API_XAI_PROXY_STRATEGY")
                or os.getenv("GROK2API_PROXY_STRATEGY")
                or strategy
            )
            source = "env"

    if not text:
        try:
            from settings_store import get_registration_config

            reg = get_registration_config(include_secrets=True) or {}
            if isinstance(reg, dict) and str(reg.get("proxy") or "").strip():
                text = str(reg.get("proxy") or "").strip()
                user = user or str(reg.get("proxy_username") or "").strip()
                password = password or str(reg.get("proxy_password") or "").strip()
                strategy = normalize_proxy_strategy(
                    str(reg.get("proxy_strategy") or strategy)
                )
                source = "registration"
        except Exception:
            pass

    if not enabled:
        return {
            "enabled": False,
            "proxy": "",
            "proxy_username": user,
            "proxy_password": password,
            "proxy_strategy": strategy,
            "source": source,
            "pool": [],
        }

    pool = parse_proxy_pool(
        text,
        username=user or None,
        password=password or None,
        fallback_env=False,
    )
    return {
        "enabled": bool(pool),
        "proxy": text,
        "proxy_username": user,
        "proxy_password": password,
        "proxy_strategy": strategy,
        "source": source if pool else "none",
        "pool": pool,
    }


def pick_proxy_for_account(
    account_id: str | None = None,
    *,
    strategy: str | None = None,
    pool: list[str] | None = None,
) -> str | None:
    """Pick a proxy for an account-pool outbound request.

    Account traffic defaults to **stable sticky-by-account** so multi-turn
    affinity keeps the same egress IP. Explicit strategies:
      - sticky: always first proxy
      - random: random each call
      - round_robin: stable hash(account_id) when account_id given, else global RR
    """
    if pool is None:
        src = get_outbound_proxy_source()
        if not src.get("enabled"):
            return None
        pool = list(src.get("pool") or [])
        if strategy is None:
            strategy = str(src.get("proxy_strategy") or "round_robin")
    pool = [str(p).strip() for p in (pool or []) if str(p).strip()]
    if not pool:
        return None
    mode = normalize_proxy_strategy(strategy)
    if mode == "sticky" or len(pool) == 1:
        return pool[0]
    if mode == "random":
        return random.choice(pool)
    # round_robin / default: pin by account id when available
    aid = str(account_id or "").strip()
    if aid:
        # FNV-1a 32-bit — fast stable hash, no crypto dependency.
        h = 2166136261
        for ch in aid.encode("utf-8", errors="ignore"):
            h ^= ch
            h = (h * 16777619) & 0xFFFFFFFF
        return pool[h % len(pool)]
    return pick_proxy(pool, strategy="round_robin")


def outbound_pool_public_summary() -> dict[str, Any]:
    src = get_outbound_proxy_source()
    pool = list(src.get("pool") or [])
    return {
        "enabled": bool(src.get("enabled") and pool),
        "count": len(pool),
        "strategy": normalize_proxy_strategy(
            str(src.get("proxy_strategy") or "round_robin")
        ),
        "source": src.get("source") or "none",
        "preview": [_mask_proxy_url(p) for p in pool[:8]],
    }
