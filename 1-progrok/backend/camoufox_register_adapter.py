from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from typing import Any, Callable, Optional

from camoufox.async_api import AsyncCamoufox

_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_CURRENT_DIR)
_VENDOR_DIR = os.path.join(_ROOT_DIR, "vendor", "grok-build-auth")
if _CURRENT_DIR not in sys.path:
    sys.path.insert(0, _CURRENT_DIR)
if _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

from sso_to_auth_json import (
    request_device_code,
    poll_token,
    token_to_auth_entry,
    import_into_project_auth,
)
from xconsole_client import YesCaptchaSolver


def run_camoufox_registration(
    sid: str,
    email: str,
    password: str,
    proxy: str,
    solver_endpoint: Optional[str],
    solver_key: str,
    provider: str,
    receiver: Any,
    update_cb: Callable[..., None],
    check_cancel_cb: Callable[[], None],
) -> dict[str, Any]:
    """Execute complete registration pipeline inside Camoufox.
    
    1. Solves Turnstile concurrently via solver.
    2. Opens sign-up page in Camoufox.
    3. Fills email & clicks 'Sign up' to trigger Castle SDK & dispatch verification email.
    4. Waits for verification code using receiver.
    5. Fills code & submits create-account.
    6. Extracts SSO session cookie.
    7. Completes Device Flow OAuth authorization.
    8. Imports OAuth tokens into ProGrok database.
    """
    # Ensure proxy loopback is respected
    proxy_url = (proxy or "").strip()
    if not proxy_url or proxy_url.lower() in ("none", "direct", "null") or "20171" in proxy_url:
        proxy_url = (proxy or os.environ.get("PROGROK_DEFAULT_PROXY") or "http://127.0.0.1:20172").strip()
    os.environ['http_proxy'] = proxy_url
    os.environ['https_proxy'] = proxy_url
    os.environ['no_proxy'] = '127.0.0.1,localhost'
    os.environ['NO_PROXY'] = '127.0.0.1,localhost'
    os.environ['GROK2API_XAI_PROXY'] = proxy_url

    async def _async_flow():
        check_cancel_cb()
        update_cb("waiting_solver", f"initializing solver ({provider})...")

        actual_endpoint = solver_endpoint
        if provider == "local" and not actual_endpoint:
            actual_endpoint = "http://127.0.0.1:5072"

        solver = YesCaptchaSolver(
            solver_key or "local",
            endpoint=actual_endpoint,
            timeout=120,
            debug=False
        )

        update_cb("solving_turnstile", f"Turnstile: starting solver ({provider})")
        loop = asyncio.get_running_loop()
        ts_task = loop.run_in_executor(
            None,
            solver.solve_turnstile,
            "https://accounts.x.ai/sign-up?redirect=grok-com",
            "0x4AAAAAAAhr9JGVDZbrZOo0"
        )

        update_cb("registering", "launching Camoufox and loading sign-up page")
        check_cancel_cb()

        async with AsyncCamoufox(headless=True, proxy={"server": proxy_url}) as browser:
            context = await browser.new_context()
            page = await context.new_page()

            captured_castle_token = None
            send_code_error = None
            send_code_success = False

            async def on_request(r):
                nonlocal captured_castle_token
                if "send-verification-code" in r.url and r.post_data:
                    try:
                        data = json.loads(r.post_data)
                        tok = data.get("castleRequestToken")
                        if tok:
                            captured_castle_token = tok
                    except Exception:
                        pass

            page.on("request", on_request)

            async def on_response(resp):
                nonlocal send_code_error, send_code_success
                if "send-verification-code" in resp.url:
                    try:
                        body = await resp.text()
                    except Exception:
                        body = ""
                    print(f"[xai-net] {email} send-verification-code status={resp.status} body={body[:150]}")
                    dom = email.split("@")[-1].strip().lower() if "@" in email else ""
                    if resp.status == 200:
                        send_code_success = True
                        send_code_error = None
                        if dom:
                            try:
                                import moemail
                                moemail.mark_domain_recovered(dom)
                            except Exception:
                                pass
                    elif resp.status >= 400 and not send_code_success:
                        send_code_error = f"HTTP {resp.status}: {body[:200]}"
                        b_lower = body.lower()
                        if dom:
                            try:
                                import moemail
                                if "email-domain-rejected" in b_lower:
                                    moemail.mark_domain_banned(dom)
                                else:
                                    # Comprehensive domain rate limit / ban detection:
                                    # Catches email-signup-unavailable, rate_limit, too_many_requests, 429, generic 400
                                    moemail.mark_domain_rate_limited(dom)
                            except Exception:
                                pass
            page.on("response", on_response)

            for nav_attempt in range(3):
                check_cancel_cb()
                try:
                    await page.goto("https://accounts.x.ai/sign-up?redirect=grok-com", timeout=45000, wait_until="domcontentloaded")
                    break
                except Exception as ne:
                    err_str = str(ne)
                    if any(k in err_str.lower() for k in ("ns_error_abort", "err_aborted", "timeout", "reset", "closed")):
                        if nav_attempt < 2:
                            print(f"[camoufox-reg] goto aborted ({err_str}), retrying {nav_attempt+2}/3...")
                            await page.wait_for_timeout(2000)
                            continue
                    raise
            check_cancel_cb()

            # Step 1: Click 'Sign up with email' and ensure email input appears (retrying for React hydration)
            update_cb("registering", f"entering email {email} and requesting code")
            email_btn = page.locator("button:has-text('Sign up with email')").first
            await email_btn.wait_for(state="visible", timeout=20000)

            email_inp = page.locator("input[type='email'], input[name='email']").first
            for click_attempt in range(12):
                check_cancel_cb()
                if await email_inp.is_visible():
                    break
                try:
                    await email_btn.click(timeout=3000)
                except Exception:
                    pass
                await page.wait_for_timeout(1500)

            await email_inp.wait_for(state="visible", timeout=15000)
            await email_inp.fill(email)
            await page.wait_for_timeout(1000)
            check_cancel_cb()

            # Step 2: Click Sign up to trigger Castle SDK and dispatch verification email (clean single click, no duplicate spam)
            signup_btn = page.locator("button:has-text('Sign up')").first
            await signup_btn.wait_for(state="visible", timeout=15000)

            try:
                await signup_btn.click(timeout=5000)
            except Exception as ce:
                print(f"[camoufox-reg] initial signup click error: {ce}")

            code_inp = page.locator("input[name='code'], input[type='text']:visible").first
            # Wait up to 10s for response or code input to appear, avoiding rapid double-clicking
            for _ in range(10):
                check_cancel_cb()
                if send_code_success or captured_castle_token or await code_inp.is_visible():
                    break
                if send_code_error:
                    break
                await page.wait_for_timeout(1000)

            # Fallback retry only if neither response nor code input appeared after 10s
            if not send_code_success and not captured_castle_token and not await code_inp.is_visible() and not send_code_error:
                try:
                    if await signup_btn.is_visible() and await signup_btn.is_enabled():
                        await signup_btn.click(timeout=3000)
                except Exception:
                    pass

            # In-session retry if first exit node hit xAI 400 frequency limit
            if send_code_error and not send_code_success and not await code_inp.is_visible():
                for retry_attempt in range(2):
                    print(f"[camoufox-reg] Node rate-limited ({send_code_error}), cycling to next exit node ({retry_attempt+1}/2)...")
                    send_code_error = None
                    await page.wait_for_timeout(1500)
                    try:
                        await page.goto("https://accounts.x.ai/sign-up?redirect=grok-com", timeout=45000, wait_until="domcontentloaded")
                        await page.wait_for_timeout(2000)
                        ebtn = page.locator("button:has-text('Sign up with email')").first
                        if await ebtn.is_visible():
                            await ebtn.click(timeout=5000)
                        einp = page.locator("input[type='email'], input[name='email']").first
                        await einp.wait_for(state="visible", timeout=15000)
                        await einp.fill(email)
                        await page.wait_for_timeout(1000)
                        sbtn = page.locator("button:has-text('Sign up')").first
                        await sbtn.click(timeout=5000)
                        for _ in range(12):
                            check_cancel_cb()
                            if send_code_success or await code_inp.is_visible():
                                break
                            if send_code_error:
                                break
                            await page.wait_for_timeout(1000)
                    except Exception as re_err:
                        print(f"[camoufox-reg] Node retry exception: {re_err}")
                    if send_code_success or await code_inp.is_visible():
                        send_code_error = None
                        break

            update_cb("waiting_email", "waiting for xAI verification code from mailbox")
            await page.wait_for_timeout(2000)

            # Wait for code using receiver
            def _wait_code():
                if hasattr(receiver, "wait_for_code"):
                    return receiver.wait_for_code(timeout=6, poll_interval=2)
                return None

            code = None
            code_deadline = time.time() + 120
            while time.time() < code_deadline:
                check_cancel_cb()
                if send_code_error and not send_code_success:
                    dom = email.split("@")[-1].strip().lower() if "@" in email else ""
                    if dom:
                        try:
                            import moemail
                            moemail.mark_domain_rate_limited(dom)
                        except Exception:
                            pass
                    raise RuntimeError(f"xAI refused to send verification code ({send_code_error})")
                try:
                    code = await loop.run_in_executor(None, _wait_code)
                except Exception as me:
                    print(f"[camoufox-reg] wait_for_code tick error: {me}")
                if code:
                    break
                await asyncio.sleep(1.5)

            if not code:
                raise RuntimeError("email verification code timeout from mailbox")

            code = str(code).strip().upper().replace(" ", "").replace("-", "")
            if len(code) != 6:
                raise RuntimeError(f"invalid verification code shape: {code!r}")

            update_cb("registering", f"code {code} received, submitting to page")
            check_cancel_cb()

            code_inp = page.locator("input[name='code'], input[type='text']:visible").first
            await code_inp.wait_for(state="visible", timeout=25000)
            await code_inp.fill(code)
            await page.wait_for_timeout(3000)
            check_cancel_cb()

            # Wait for Turnstile token
            update_cb("solving_turnstile", "awaiting solved Turnstile token")
            ts_token = await ts_task
            check_cancel_cb()

            # Call create-account
            update_cb("creating_account", "submitting create-account via browser context")
            eval_result = await page.evaluate("""async (args) => {
                let { email, password, givenName, familyName, code, turnstileToken, castleToken } = args;
                try {
                    if (window.Castle && typeof window.Castle.createRequestToken === 'function') {
                        try {
                            const freshTok = await window.Castle.createRequestToken();
                            if (freshTok) castleToken = freshTok;
                        } catch (ce) {}
                    }

                    const resp = await fetch('https://accounts.x.ai/api/auth/sign-up/create-account', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        credentials: 'same-origin',
                        body: JSON.stringify({
                            email: email,
                            password: password,
                            givenName: givenName,
                            familyName: familyName,
                            emailValidationCode: code,
                            turnstileToken: turnstileToken,
                            castleRequestToken: castleToken,
                            marketingEmailOptIn: false
                        })
                    });
                    const text = await resp.text();
                    let json = null;
                    try { json = JSON.parse(text); } catch (e) {}
                    return {
                        status: resp.status,
                        ok: resp.ok,
                        text: text,
                        json: json
                    };
                } catch (err) {
                    return { error: String(err) };
                }
            }""", {
                "email": email,
                "password": password,
                "givenName": "Grok",
                "familyName": "User",
                "code": code,
                "turnstileToken": ts_token,
                "castleToken": captured_castle_token
            })

            if not eval_result.get("ok"):
                status_code = eval_result.get("status")
                err_text = eval_result.get("text") or eval_result.get("error") or "empty response"
                raise RuntimeError(f"create-account failed (HTTP {status_code}): {err_text}")

            dom = email.split("@")[-1].strip().lower() if "@" in email else ""
            if dom:
                try:
                    import moemail
                    moemail.mark_domain_recovered(dom)
                except Exception:
                    pass

            await page.wait_for_timeout(3000)
            check_cancel_cb()

            # Extract SSO cookie
            update_cb("fetching_sso", "extracting SSO session from cookies")
            cookies = await context.cookies()
            sso_value = None
            for c in cookies:
                if c['name'] in ('sso', 'sso-rw'):
                    sso_value = c['value']
                    break

            if not sso_value:
                raise RuntimeError("SSO cookie not found in browser cookies")

            # Device Flow Authorization in Camoufox
            update_cb("importing", "performing Device Flow OAuth authorization")
            check_cancel_cb()

            import curl_cffi.requests as c_requests
            curl_sess = c_requests.Session(proxy=proxy_url, impersonate="chrome")

            dc = await loop.run_in_executor(None, lambda: request_device_code(session=curl_sess, proxy=proxy_url))
            if not dc:
                # Direct fallback with dedicated 20172 pool via urllib
                dc = await loop.run_in_executor(None, lambda: request_device_code(proxy=proxy_url))
            if not dc:
                raise RuntimeError("request_device_code failed")

            for nav_attempt in range(3):
                check_cancel_cb()
                try:
                    await page.goto(dc["verification_uri_complete"], timeout=30000, wait_until="domcontentloaded")
                    break
                except Exception as ne:
                    err_str = str(ne)
                    if any(k in err_str.lower() for k in ("ns_error_abort", "err_aborted", "timeout", "reset", "closed")):
                        if nav_attempt < 2:
                            await page.wait_for_timeout(2000)
                            continue
                    raise

            for step in range(6):
                await page.wait_for_timeout(2000)
                if "done" in page.url.lower():
                    break
                btn_clicked = False
                try:
                    btn = page.locator("button[type='submit']:has-text('Continue'), button:has-text('Continue'), button:has-text('Authorize'), button:has-text('Confirm'), button:has-text('Allow')").first
                    if await btn.is_visible():
                        await btn.click(timeout=5000)
                        btn_clicked = True
                except Exception:
                    pass
                if not btn_clicked:
                    try:
                        for b in await page.query_selector_all("button"):
                            try:
                                txt = (await b.inner_text()).strip()
                                if txt.lower() in ("allow", "continue", "authorize", "confirm"):
                                    await b.click(timeout=5000)
                                    btn_clicked = True
                                    break
                            except Exception:
                                continue
                    except Exception:
                        pass
                if not btn_clicked and step > 2:
                    break

            await page.wait_for_timeout(2000)
            check_cancel_cb()

            update_cb("importing", "polling OAuth token from x.ai")
            token = await loop.run_in_executor(
                None,
                lambda: poll_token(
                    dc["device_code"],
                    dc.get("interval", 1),
                    1800,
                    45,
                    session=curl_sess,
                    proxy=proxy_url,
                    immediate=True,
                ),
            )
            if not token or not token.get("access_token"):
                raise RuntimeError("Device Flow poll_token failed")

            update_cb("importing", "importing account into ProGrok database")
            auth_key, entry = token_to_auth_entry(token, email=email)
            entry["sso"] = sso_value
            entry["password"] = password
            imported_key = import_into_project_auth(entry)

            return {
                "ok": True,
                "token": token,
                "sso": sso_value,
                "imported_key": imported_key
            }

    return asyncio.run(_async_flow())
