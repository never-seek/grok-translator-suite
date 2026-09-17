"""Post-registration account probe and remote import helpers."""
from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from export_formats import (
    CPA_BASE_URL,
    build_cpa_record,
    build_sub2api_payload,
    cpa_filename,
)

DEFAULT_PROBE_MODEL = "grok-4.5"
PROBE_PERMISSION_RETRY_DELAYS = (5.0, 10.0, 20.0)
PROBE_RESPONSE_TIMEOUT = httpx.Timeout(75.0, connect=10.0)
PROBE_MODELS_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
PROBE_TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_GROK2API_ADMIN_TOKEN_LOCK = threading.RLock()
_GROK2API_ADMIN_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def _cpa_api_base_url(value: str) -> str:
    """Accept either the CPA site root or its management.html page URL."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    path = parsed.path.rstrip("/")
    if path.endswith("/management.html"):
        path = path[: -len("/management.html")]
    elif path.endswith("/v0/management"):
        path = path[: -len("/v0/management")]
    return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            return str(data.get("message") or data.get("error") or data.get("detail") or "")[:300]
    except Exception:
        pass
    return f"HTTP {response.status_code}"


def _is_transient_permission_error(status_code: int, error: str) -> bool:
    text = str(error or "").lower()
    return status_code == 403 and (
        "access to the chat endpoint is denied" in text
        or "log into console.x.ai and update the permissions" in text
    )


def _models_from_payload(payload: Any) -> list[Any]:
    if not isinstance(payload, dict):
        return []
    models = payload.get("data")
    if not isinstance(models, list):
        models = payload.get("models")
    return models if isinstance(models, list) else []


def _lightweight_probe(
    http: httpx.Client,
    *,
    base_url: str,
    headers: dict[str, str],
    model: str,
    started: float,
    response_error: str,
    response_status_code: int | None = None,
) -> dict[str, Any]:
    """Fallback to /models so slow inference is not mistaken for a dead account."""
    try:
        response = http.get(
            f"{base_url}/models",
            headers=headers,
            timeout=PROBE_MODELS_TIMEOUT,
        )
        latency_ms = int((time.time() - started) * 1000)
        if response.status_code < 400:
            try:
                models = _models_from_payload(response.json())
            except Exception:
                models = []
            if models:
                return {
                    "ok": True,
                    "available": True,
                    "chat_ready": False,
                    "degraded": True,
                    "fallback": "models",
                    "classification": "model_busy",
                    "model": model,
                    "status_code": response.status_code,
                    "response_status_code": response_status_code,
                    "latency_ms": latency_ms,
                    "retry_count": 0,
                    "message": "消息响应异常，但轻量模型验证通过",
                    "response_error": response_error[:220],
                }
            return {
                "ok": False,
                "available": None,
                "retryable": True,
                "classification": "uncertain",
                "fallback": "models",
                "model": model,
                "status_code": response.status_code,
                "response_status_code": response_status_code,
                "latency_ms": latency_ms,
                "error": "消息响应异常，且 /models 暂未返回可用模型",
            }
        error = _error_text(response)
        if response.status_code == 401:
            return {
                "ok": False,
                "available": False,
                "retryable": False,
                "classification": "invalid_credential",
                "fallback": "models",
                "model": model,
                "status_code": response.status_code,
                "response_status_code": response_status_code,
                "latency_ms": latency_ms,
                "error": error or "认证凭证无效",
            }
        retryable = response.status_code in PROBE_TRANSIENT_STATUS_CODES or response.status_code == 403
        return {
            "ok": False,
            "available": None if retryable else False,
            "retryable": retryable,
            "classification": "uncertain" if retryable else "probe_failed",
            "fallback": "models",
            "model": model,
            "status_code": response.status_code,
            "response_status_code": response_status_code,
            "latency_ms": latency_ms,
            "error": error or response_error,
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "available": None,
            "retryable": True,
            "classification": "uncertain",
            "fallback": "models",
            "model": model,
            "response_status_code": response_status_code,
            "latency_ms": int((time.time() - started) * 1000),
            "error": f"消息与轻量验证均暂时无响应：{str(exc)[:180]}",
        }


def probe_account(
    record: dict[str, Any],
    model: str = DEFAULT_PROBE_MODEL,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Run the Grok Build /responses probe used by grokcli-2api."""
    token = str(record.get("access_token") or record.get("key") or "").strip()
    model = str(model or DEFAULT_PROBE_MODEL).strip() or DEFAULT_PROBE_MODEL
    if not token:
        return {"ok": False, "available": False, "model": model, "error": "缺少 access_token"}

    base_url = str(record.get("base_url") or CPA_BASE_URL).rstrip("/")
    if "api.x.ai" in base_url:
        base_url = CPA_BASE_URL.rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "grok-cli/0.2.93",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-grok-client-version": "0.2.93",
        "x-grok-client-identifier": "grok-shell",
    }
    extra_headers = record.get("headers")
    if isinstance(extra_headers, dict):
        for key, value in extra_headers.items():
            if isinstance(key, str) and isinstance(value, str) and key.strip():
                headers[key] = value
    body = {
        "model": model,
        "input": "Reply exactly: OK",
        "max_output_tokens": 8,
    }
    started = time.time()
    owned = client is None
    http = client or httpx.Client(timeout=PROBE_RESPONSE_TIMEOUT)
    try:
        retry_count = 0
        while True:
            response = http.post(
                f"{base_url}/responses",
                headers=headers,
                json=body,
                timeout=PROBE_RESPONSE_TIMEOUT,
            )
            latency_ms = int((time.time() - started) * 1000)
            if response.status_code < 400:
                return {
                    "ok": True,
                    "available": True,
                    "model": model,
                    "status_code": response.status_code,
                    "latency_ms": latency_ms,
                    "retry_count": retry_count,
                }
            error = _error_text(response)
            if response.status_code == 401:
                return {
                    "ok": False,
                    "available": False,
                    "retryable": False,
                    "classification": "invalid_credential",
                    "model": model,
                    "status_code": response.status_code,
                    "latency_ms": latency_ms,
                    "retry_count": retry_count,
                    "error": error,
                }
            if (
                retry_count >= len(PROBE_PERMISSION_RETRY_DELAYS)
                or not _is_transient_permission_error(response.status_code, error)
            ):
                return _lightweight_probe(
                    http,
                    base_url=base_url,
                    headers=headers,
                    model=model,
                    started=started,
                    response_error=error,
                    response_status_code=response.status_code,
                )
            time.sleep(PROBE_PERMISSION_RETRY_DELAYS[retry_count])
            retry_count += 1
    except httpx.HTTPError as exc:
        return _lightweight_probe(
            http,
            base_url=base_url,
            headers=headers,
            model=model,
            started=started,
            response_error=f"网络错误：{str(exc)[:220]}",
        )
    finally:
        if owned:
            http.close()


def import_to_cpa(
    record: dict[str, Any],
    *,
    base_url: str,
    api_key: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    base = _cpa_api_base_url(base_url)
    key = str(api_key or "").strip()
    if not base or not key:
        return {"ok": False, "target": "cpa", "error": "CPA 地址或管理密钥未填写"}
    cpa = build_cpa_record(record)
    name = cpa_filename(cpa)
    owned = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        response = http.post(
            f"{base}/v0/management/auth-files?name={quote(name)}",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=cpa,
        )
        if response.status_code >= 300:
            return {"ok": False, "target": "cpa", "status_code": response.status_code, "error": _error_text(response)}
        return {"ok": True, "target": "cpa", "status_code": response.status_code, "filename": name}
    except httpx.HTTPError as exc:
        return {"ok": False, "target": "cpa", "error": f"网络错误：{str(exc)[:220]}"}
    finally:
        if owned:
            http.close()


def import_to_sub2api(
    record: dict[str, Any],
    *,
    base_url: str,
    api_key: str = "",
    auth_mode: str = "password",
    admin_email: str = "",
    admin_password: str = "",
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    base = str(base_url or "").strip().rstrip("/")
    key = str(api_key or "").strip()
    mode = str(auth_mode or "password").strip().lower()
    email = str(admin_email or "").strip()
    password = str(admin_password or "")
    if not base:
        return {"ok": False, "target": "sub2api", "error": "Sub2API 地址未填写"}
    if mode == "api_key" and not key:
        return {"ok": False, "target": "sub2api", "error": "Sub2API 管理员 API Key 未填写"}
    if mode == "password" and (not email or not password):
        return {"ok": False, "target": "sub2api", "error": "Sub2API 管理员邮箱或密码未填写"}
    if mode not in {"password", "api_key"}:
        return {"ok": False, "target": "sub2api", "error": "不支持的 Sub2API 认证方式"}
    owned = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        if mode == "password":
            login_response = http.post(
                f"{base}/api/v1/auth/login",
                headers={"Content-Type": "application/json"},
                json={"email": email, "password": password},
            )
            if login_response.status_code >= 300:
                return {
                    "ok": False,
                    "target": "sub2api",
                    "status_code": login_response.status_code,
                    "error": f"Sub2API 登录失败：{_error_text(login_response)}",
                }
            try:
                login_payload = login_response.json()
            except Exception:
                login_payload = {}
            login_data = (
                login_payload.get("data")
                if isinstance(login_payload, dict) and isinstance(login_payload.get("data"), dict)
                else login_payload
            )
            if isinstance(login_data, dict) and login_data.get("requires_2fa"):
                return {
                    "ok": False,
                    "target": "sub2api",
                    "error": "Sub2API 管理员账号启用了二次验证，请改用管理员 API Key",
                }
            access_token = (
                str(login_data.get("access_token") or "").strip()
                if isinstance(login_data, dict)
                else ""
            )
            if not access_token:
                return {
                    "ok": False,
                    "target": "sub2api",
                    "error": "Sub2API 登录响应中没有 access_token",
                }
            auth_headers = {"Authorization": f"Bearer {access_token}"}
        else:
            auth_headers = {"x-api-key": key}
        response = http.post(
            f"{base}/api/v1/admin/accounts/data",
            headers={**auth_headers, "Content-Type": "application/json"},
            json={"data": build_sub2api_payload([record]), "skip_default_group_bind": False},
        )
        if response.status_code >= 300:
            return {"ok": False, "target": "sub2api", "status_code": response.status_code, "error": _error_text(response)}
        try:
            payload = response.json()
        except Exception:
            payload = {}
        result = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
        failed = int(result.get("account_failed") or 0) if isinstance(result, dict) else 0
        if failed:
            return {"ok": False, "target": "sub2api", "status_code": response.status_code, "error": f"Sub2API 导入失败账号数：{failed}"}
        created_value = result.get("account_created") if isinstance(result, dict) else None
        created = int(created_value) if created_value is not None else 1
        return {"ok": True, "target": "sub2api", "status_code": response.status_code, "created": created}
    except httpx.HTTPError as exc:
        return {"ok": False, "target": "sub2api", "error": f"网络错误：{str(exc)[:220]}"}
    finally:
        if owned:
            http.close()


def _grokcli2api_base_url(value: str) -> str:
    raw = str(value or '').strip() or 'http://127.0.0.1:3000'
    raw = raw.rstrip('/')
    for suffix in ('/v1', '/admin'):
        if raw.lower().endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    return raw.rstrip('/') or 'http://127.0.0.1:3000'


def import_to_grokcli2api(
    record: dict[str, Any],
    *,
    base_url: str = 'http://127.0.0.1:3000',
    admin_password: str = '',
    admin_token: str = '',
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    base = _grokcli2api_base_url(base_url)
    token = str(admin_token or '').strip()
    password = str(admin_password or '')
    if not token and not password:
        return {'ok': False, 'target': 'grokcli2api', 'error': 'grokcli2api admin password/token missing'}
    owned = client is None
    http = client or httpx.Client(timeout=60.0)
    try:
        if not token:
            login = http.post(f'{base}/admin/api/login', headers={'Content-Type': 'application/json'}, json={'password': password})
            if login.status_code >= 300:
                return {'ok': False, 'target': 'grokcli2api', 'status_code': login.status_code, 'error': f'grokcli2api login failed: {_error_text(login)}'}
            try:
                payload = login.json()
            except Exception:
                payload = {}
            token = str(payload.get('token') or payload.get('session_token') or payload.get('session') or '').strip()
            if not token:
                return {'ok': False, 'target': 'grokcli2api', 'error': 'grokcli2api login returned no token'}
        response = http.post(f'{base}/admin/api/accounts/import', headers={'X-Admin-Token': token, 'Content-Type': 'application/json'}, json={'payload': record, 'merge': True})
        if response.status_code >= 300:
            return {'ok': False, 'target': 'grokcli2api', 'status_code': response.status_code, 'error': _error_text(response)}
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload.get('ok') is False:
            return {'ok': False, 'target': 'grokcli2api', 'status_code': response.status_code, 'error': str(payload.get('error') or payload.get('detail') or 'import failed')[:300]}
        imported = payload.get('imported') if isinstance(payload, dict) else None
        created = len(imported) if isinstance(imported, list) else (int(payload.get('count') or 1) if isinstance(payload, dict) else 1)
        return {'ok': True, 'target': 'grokcli2api', 'status_code': response.status_code, 'created': created, 'total_accounts': payload.get('total_accounts') if isinstance(payload, dict) else None}
    except httpx.HTTPError as exc:
        return {'ok': False, 'target': 'grokcli2api', 'error': f'network error: {str(exc)[:220]}'}
    finally:
        if owned:
            http.close()


def _grok2api_base_url(value: str) -> str:
    raw = str(value or "").strip() or "http://127.0.0.1:3001"
    raw = raw.rstrip("/")
    for suffix in ("/v1", "/admin", "/api/admin/v1"):
        if raw.lower().endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    return raw.rstrip("/") or "http://127.0.0.1:3001"


def _extract_grok2api_admin_token(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    tokens = data.get("tokens") if isinstance(data, dict) and isinstance(data.get("tokens"), dict) else {}
    for key in ("accessToken", "access_token", "token"):
        value = tokens.get(key) if isinstance(tokens, dict) else None
        if value:
            return str(value).strip()
    for key in ("accessToken", "access_token", "token"):
        value = data.get(key) if isinstance(data, dict) else None
        if value:
            return str(value).strip()
    return ""


def _cached_grok2api_admin_token(
    http: httpx.Client,
    *,
    base: str,
    username: str,
    password: str,
) -> tuple[str, dict[str, Any] | None]:
    cache_key = "\x00".join([base, username, password])
    now = time.time()
    with _GROK2API_ADMIN_TOKEN_LOCK:
        cached = _GROK2API_ADMIN_TOKEN_CACHE.get(cache_key)
        if cached and cached[1] > now + 30:
            return cached[0], None
        login = http.post(
            f"{base}/api/admin/v1/auth/login",
            headers={"Content-Type": "application/json"},
            json={"username": username, "password": password},
            timeout=httpx.Timeout(15.0, connect=5.0, read=10.0, write=5.0, pool=5.0),
        )
        if login.status_code >= 300:
            return "", {
                "ok": False,
                "target": "grok2api",
                "status_code": login.status_code,
                "error": f"grok2api login failed: {_error_text(login)}",
            }
        try:
            token = _extract_grok2api_admin_token(login.json())
        except Exception:
            token = ""
        if not token:
            return "", {"ok": False, "target": "grok2api", "error": "grok2api login returned no access token"}
        # Default config has a 15 minute access token TTL. Refresh a little early.
        _GROK2API_ADMIN_TOKEN_CACHE[cache_key] = (token, now + 12 * 60)
        return token, None


def _clear_grok2api_admin_token(*, base: str, username: str, password: str) -> None:
    cache_key = "\x00".join([base, username, password])
    with _GROK2API_ADMIN_TOKEN_LOCK:
        _GROK2API_ADMIN_TOKEN_CACHE.pop(cache_key, None)


def _parse_grok2api_import_events(text: str) -> dict[str, Any]:
    complete: dict[str, Any] | None = None
    errors: list[str] = []
    for block in str(text or "").replace("\r\n", "\n").split("\n\n"):
        event = "message"
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())
        if not data_lines:
            continue
        data_text = "\n".join(data_lines).strip()
        try:
            payload = json.loads(data_text)
        except Exception:
            payload = {"message": data_text}
        if event == "error":
            if isinstance(payload, dict):
                errors.append(str(payload.get("message") or payload.get("error") or payload)[:300])
            else:
                errors.append(str(payload)[:300])
        elif event == "complete":
            complete = payload if isinstance(payload, dict) else {"message": str(payload)}
    if errors:
        return {"ok": False, "error": "; ".join(errors)[:500]}
    if complete is not None:
        return {"ok": True, **complete}
    stripped = str(text or "").strip()
    if stripped:
        try:
            payload = json.loads(stripped)
            if isinstance(payload, dict):
                return {"ok": payload.get("ok") is not False, **payload}
        except Exception:
            pass
    return {"ok": True}


def _parse_grok2api_sse_data(data_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(str(data_text or "").strip())
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {"message": str(data_text or "").strip()}


def _grok2api_import_stream(
    http: httpx.Client,
    *,
    url: str,
    headers: dict[str, str],
    files: dict[str, Any],
    provider: str,
) -> dict[str, Any]:
    """Import via grok2api SSE without waiting forever on post-import sync.

    chenyme/grok2api writes the account first, then runs quota/model sync in
    the same event stream. Build sync can keep the stream alive with heartbeats
    for a long time when egress nodes are bad, which blocks ProGrok's importer.
    Once the importing phase reaches completed=total, the account is already in
    grok2api, so a later read timeout is treated as sync_deferred instead of an
    import failure.
    """
    timeout = httpx.Timeout(20.0, connect=5.0, read=8.0, write=10.0, pool=5.0)
    status_code: int | None = None
    event = "message"
    data_lines: list[str] = []
    import_done = False
    import_total = 0
    created_hint = 0
    updated_hint = 0
    sync_seen = False
    sync_completed = 0
    sync_total = 0
    errors: list[str] = []

    def finish_block() -> dict[str, Any] | None:
        nonlocal event, data_lines, import_done, import_total
        nonlocal created_hint, updated_hint, sync_seen, sync_completed, sync_total
        if not data_lines:
            event = "message"
            return None
        payload = _parse_grok2api_sse_data("\n".join(data_lines))
        current_event = event
        event = "message"
        data_lines = []
        if current_event == "error":
            errors.append(str(payload.get("message") or payload.get("error") or payload)[:300])
            return {"ok": False, "error": "; ".join(errors)[:500], "status_code": status_code}
        if current_event == "complete":
            return {"ok": True, "status_code": status_code, **payload}
        if current_event == "progress":
            phase = str(payload.get("phase") or "").lower()
            try:
                completed = int(payload.get("completed") or 0)
            except (TypeError, ValueError):
                completed = 0
            try:
                total = int(payload.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
            if phase == "importing" and total > 0:
                import_total = max(import_total, total)
                if completed >= total:
                    import_done = True
                    created_hint = max(created_hint, total)
                    return {
                        "ok": True,
                        "provider": provider,
                        "status_code": status_code,
                        "created": created_hint or import_total or 1,
                        "updated": updated_hint,
                        "synced": 0,
                        "syncFailed": 0,
                        "sync_deferred": True,
                    }
            elif phase == "syncing":
                sync_seen = True
                sync_completed = max(sync_completed, completed)
                sync_total = max(sync_total, total)
        return None

    try:
        with http.stream("POST", url, headers=headers, files=files, timeout=timeout) as response:
            status_code = response.status_code
            if response.status_code >= 300:
                return {
                    "ok": False,
                    "provider": provider,
                    "status_code": response.status_code,
                    "error": _error_text(response),
                }
            for line in response.iter_lines():
                if line == "":
                    result = finish_block()
                    if result is not None:
                        return result
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                    continue
                if line.startswith("data:"):
                    data_lines.append(line.split(":", 1)[1].lstrip())
            result = finish_block()
            if result is not None:
                return result
    except httpx.ReadTimeout as exc:
        if not import_done:
            return {
                "ok": False,
                "provider": provider,
                "status_code": status_code,
                "error": f"grok2api import stream read timeout before import completion: {str(exc)[:180]}",
            }
    except httpx.HTTPError as exc:
        if not import_done:
            return {
                "ok": False,
                "provider": provider,
                "status_code": status_code,
                "error": f"network error: {str(exc)[:220]}",
            }

    if import_done:
        return {
            "ok": True,
            "provider": provider,
            "status_code": status_code,
            "created": created_hint or import_total or 1,
            "updated": updated_hint,
            "synced": sync_completed if sync_seen else 0,
            "syncFailed": max(0, sync_total - sync_completed) if sync_seen and sync_total else 0,
            "sync_deferred": True,
        }
    if errors:
        return {"ok": False, "provider": provider, "status_code": status_code, "error": "; ".join(errors)[:500]}
    return {"ok": False, "provider": provider, "status_code": status_code, "error": "grok2api import stream ended before import completion"}


def _non_empty_mapping(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None and str(v).strip() != ""}


def _grok2api_provider_payload(record: dict[str, Any], provider: str) -> dict[str, Any] | None:
    provider = str(provider or "").strip().lower()
    email = str(record.get("email") or "").strip()
    name = str(record.get("name") or email or "progrok-account").strip()
    if provider == "build":
        if not (record.get("access_token") or record.get("refresh_token") or record.get("key")):
            return None
        entry = _non_empty_mapping(
            {
                "provider": "grok_build",
                "name": name,
                "client_id": record.get("client_id") or record.get("oidc_client_id"),
                "access_token": record.get("access_token") or record.get("key"),
                "refresh_token": record.get("refresh_token"),
                "id_token": record.get("id_token"),
                "token_type": record.get("token_type") or "Bearer",
                "scope": record.get("scope"),
                "expires_at": record.get("expires_at"),
                "email": email,
                "sub": record.get("sub"),
                "user_id": record.get("user_id") or record.get("principal_id"),
                "principal_id": record.get("principal_id"),
                "team_id": record.get("team_id"),
            }
        )
        return {"provider": "grok_build", "accounts": [entry]}
    sso = str(
        record.get("sso")
        or record.get("sso_token")
        or record.get("token")
        or record.get("cookie")
        or ""
    ).strip()
    if not sso:
        return None
    if sso.lower().startswith("sso="):
        sso = sso[4:].split(";", 1)[0].strip()
    cloudflare_cookies = str(
        record.get("cloudflare_cookies") or record.get("cf_cookies") or ""
    ).strip()
    if provider == "web":
        entry = _non_empty_mapping(
            {
                "name": name,
                "email": email,
                "user_id": record.get("user_id") or record.get("principal_id"),
                "sso_token": sso,
                "tier": record.get("tier") or "auto",
                "cloudflare_cookies": cloudflare_cookies,
                "nsfw_enabled_at": record.get("nsfw_enabled_at"),
                "tos_accepted_at": record.get("tos_accepted_at"),
                "tos_version": record.get("tos_version"),
                "birth_date_set_at": record.get("birth_date_set_at"),
            }
        )
        return {"provider": "grok_web", "accounts": [entry]}
    if provider == "console":
        entry = _non_empty_mapping(
            {
                "name": name,
                "email": email,
                "user_id": record.get("user_id") or record.get("principal_id"),
                "sso_token": sso,
                "cloudflare_cookies": cloudflare_cookies,
            }
        )
        return {"provider": "grok_console", "accounts": [entry]}
    return None


def _grok2api_providers(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_parts = [str(x) for x in value]
    else:
        raw = str(value or "build,web,console")
        for sep in ("+", ";", "\n", "\r", "\t", " "):
            raw = raw.replace(sep, ",")
        raw_parts = raw.split(",")
    aliases = {
        "all": ["build", "web", "console"],
        "default": ["build", "web", "console"],
        "grok_build": ["build"],
        "build": ["build"],
        "cli": ["build"],
        "grok_web": ["web"],
        "web": ["web"],
        "grok_console": ["console"],
        "console": ["console"],
    }
    out: list[str] = []
    seen: set[str] = set()
    for part in raw_parts:
        for provider in aliases.get(str(part).strip().lower(), []):
            if provider not in seen:
                seen.add(provider)
                out.append(provider)
    return out or ["build", "web", "console"]


def import_to_grok2api(
    record: dict[str, Any],
    *,
    base_url: str = "http://127.0.0.1:3001",
    admin_username: str = "admin",
    admin_password: str = "",
    admin_token: str = "",
    providers: Any = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    base = _grok2api_base_url(base_url)
    token = str(admin_token or "").strip()
    username = str(admin_username or "admin").strip() or "admin"
    password = str(admin_password or "")
    if not token and not password:
        return {"ok": False, "target": "grok2api", "error": "grok2api admin password/token missing"}
    owned = client is None
    http = client or httpx.Client(timeout=90.0)
    try:
        if not token:
            token, login_error = _cached_grok2api_admin_token(
                http, base=base, username=username, password=password
            )
            if login_error is not None:
                return login_error

        endpoints = {
            "build": "/api/admin/v1/accounts/import",
            "web": "/api/admin/v1/accounts/web/import",
            "console": "/api/admin/v1/accounts/console/import",
        }
        results: list[dict[str, Any]] = []
        for provider in _grok2api_providers(providers):
            payload = _grok2api_provider_payload(record, provider)
            if payload is None:
                results.append({"ok": False, "provider": provider, "error": f"record lacks {provider} credentials"})
                continue
            filename = f"progrok-{provider}-{int(time.time())}.json"
            parsed = _grok2api_import_stream(
                http,
                url=f"{base}{endpoints[provider]}",
                headers={"Authorization": f"Bearer {token}"},
                files={
                    "file": (
                        filename,
                        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        "application/json",
                    )
                },
                provider=provider,
            )
            if parsed.get("status_code") == 401 and not admin_token:
                _clear_grok2api_admin_token(base=base, username=username, password=password)
            results.append(
                {
                    "ok": bool(parsed.get("ok")),
                    "provider": provider,
                    "status_code": parsed.get("status_code"),
                    "created": int(parsed.get("created") or 0),
                    "updated": int(parsed.get("updated") or 0),
                    "synced": int(parsed.get("synced") or 0),
                    "sync_failed": int(parsed.get("syncFailed") or parsed.get("sync_failed") or 0),
                    "sync_deferred": bool(parsed.get("sync_deferred")),
                    "error": str(parsed.get("error") or "")[:300] or None,
                }
            )
        ok = bool(results) and all(item.get("ok") for item in results)
        errors = [f"{item.get('provider')}: {item.get('error')}" for item in results if item.get("error")]
        return {
            "ok": ok,
            "target": "grok2api",
            "results": results,
            "created": sum(int(item.get("created") or 0) for item in results),
            "updated": sum(int(item.get("updated") or 0) for item in results),
            "error": "; ".join(errors)[:500] if errors else None,
        }
    except httpx.HTTPError as exc:
        return {"ok": False, "target": "grok2api", "error": f"network error: {str(exc)[:220]}"}
    finally:
        if owned:
            http.close()


def _normalize_import_target(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        parts = [str(x) for x in value]
    else:
        raw = str(value or "sub2api").strip().lower()
        for sep in ("+", ";", "\n", "\r", "\t", " "):
            raw = raw.replace(sep, ",")
        parts = raw.split(",")
    aliases = {
        "all": ["grokcli2api", "grok2api"],
        "both": ["grokcli2api", "grok2api"],
        "2api": ["grokcli2api", "grok2api"],
        "grokcli2api": ["grokcli2api"],
        "grokcli-2api": ["grokcli2api"],
        "hm2899": ["grokcli2api"],
        "grok2api": ["grok2api"],
        "chenyme": ["grok2api"],
        "chenyme-grok2api": ["grok2api"],
        "cpa": ["cpa"],
        "sub2api": ["sub2api"],
    }
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        for target in aliases.get(str(part).strip().lower(), []):
            if target not in seen:
                seen.add(target)
                out.append(target)
    return out or ["sub2api"]


def _import_account_single(record: dict[str, Any], config: dict[str, Any], target: str) -> dict[str, Any]:
    if target == "cpa":
        return import_to_cpa(
            record,
            base_url=str(config.get("cpa_base_url") or ""),
            api_key=str(config.get("cpa_management_key") or ""),
        )
    if target == "sub2api":
        return import_to_sub2api(
            record,
            base_url=str(config.get("sub2api_base_url") or ""),
            api_key=str(config.get("sub2api_api_key") or ""),
            auth_mode=str(config.get("sub2api_auth_mode") or "password"),
            admin_email=str(config.get("sub2api_admin_email") or ""),
            admin_password=str(config.get("sub2api_admin_password") or ""),
        )
    if target in {"grok2api", "chenyme", "chenyme-grok2api"}:
        return import_to_grok2api(
            record,
            base_url=str(config.get("grok2api_base_url") or "http://127.0.0.1:3001"),
            admin_username=str(config.get("grok2api_admin_username") or "admin"),
            admin_password=str(config.get("grok2api_admin_password") or ""),
            admin_token=str(config.get("grok2api_admin_token") or ""),
            providers=config.get("grok2api_providers") or "build,web,console",
        )
    if target in {"grokcli2api", "grokcli-2api", "hm2899"}:
        return import_to_grokcli2api(
            record,
            base_url=str(config.get("grokcli2api_base_url") or "http://127.0.0.1:3000"),
            admin_password=str(config.get("grokcli2api_admin_password") or ""),
            admin_token=str(config.get("grokcli2api_admin_token") or ""),
        )
    return {"ok": False, "target": target, "error": "unsupported auto import target"}


def import_account(record: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    target_value = config.get("targets") if config.get("targets") else config.get("target")
    targets = _normalize_import_target(target_value)
    if len(targets) == 1:
        return _import_account_single(record, config, targets[0])
    results = [_import_account_single(record, config, target) for target in targets]
    ok = bool(results) and all(item.get("ok") for item in results)
    errors = [
        f"{item.get('target') or targets[index]}: {item.get('error')}"
        for index, item in enumerate(results)
        if item.get("error")
    ]
    return {
        "ok": ok,
        "target": "multi",
        "targets": targets,
        "results": results,
        "created": sum(int(item.get("created") or 0) for item in results),
        "updated": sum(int(item.get("updated") or 0) for item in results),
        "error": "; ".join(errors)[:500] if errors else None,
    }
