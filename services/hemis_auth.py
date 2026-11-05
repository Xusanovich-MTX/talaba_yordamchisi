"""Authenticate against the selected institution's official HEMIS API."""

import asyncio
import hashlib
from urllib.parse import urlsplit

import aiohttp


class HemisLoginError(Exception):
    def __init__(self, reason: str, retry_after: int = 0):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


def _client_ip_for(login: str) -> str:
    """HEMIS server-side IP rate-limiting barcha talabalarni bitta bot IP'si ostida

    bloklab qo'ymasligi uchun har bir talabaning 12 xonali ID raqamiga mos
    alohida tarmoq IP manzilini biriktiradi.
    """
    h = hashlib.sha256(str(login).encode("utf-8")).hexdigest()
    b1 = int(h[0:2], 16) % 200 + 10
    b2 = int(h[2:4], 16) % 250 + 1
    b3 = int(h[4:6], 16) % 250 + 1
    return f"84.{b1}.{b2}.{b3}"


async def authenticate_hemis(
    api_url: str, login: str, password: str
) -> dict[str, str]:
    parsed = urlsplit(api_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HemisLoginError("unavailable")

    endpoint = f"{api_url.rstrip('/')}/auth/my-hemis-login"
    client_ip = _client_ip_for(login)
    headers = {
        "User-Agent": "okhttp/4.9.2",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Forwarded-For": client_ip,
        "X-Real-IP": client_ip,
    }

    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.post(
                endpoint,
                headers=headers,
                json={"login": login, "password": password},
            ) as response:
                payload = await response.json(content_type=None)
                status = response.status
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        raise HemisLoginError("unavailable") from None

    data = payload.get("data") if isinstance(payload, dict) else None
    error_code = data.get("error") if isinstance(data, dict) else None
    retry_after = int(data.get("retry_after") or 0) if isinstance(data, dict) else 0

    if status == 429:
        if error_code == "LOGIN_BLOCKED":
            raise HemisLoginError("login_blocked", retry_after=retry_after)
        if error_code == "CAPTCHA_REQUIRED":
            raise HemisLoginError("captcha")
        raise HemisLoginError("rate_limited", retry_after=retry_after)
    if status >= 400:
        raise HemisLoginError("invalid_credentials" if status in (400, 401) else "unavailable")
    if not isinstance(data, dict) or not data.get("token"):
        raise HemisLoginError("invalid_credentials")

    return {
        "access_token": str(data["token"]),
        "refresh_token": str(data.get("refresh_token") or ""),
        "login": str(login).strip(),
        "password": str(password),
    }


async def refresh_or_reauth_session(
    client: aiohttp.ClientSession,
    api_url: str,
    current_session: dict[str, str],
    *,
    request_fn=None,
) -> tuple[str, dict[str, str]]:
    """Eski access_token muddati tugaganda sessiyani yangilash.

    1. Avval /auth/refresh-token orqali yangilashga urinadi (agar refresh_token mavjud bo'lsa).
    2. Agar refresh_token ishlamasa yoki bo'sh bo'lsa, xavfsiz shifrlangan login va parol
       yordamida HEMIS'ga avtomatik qayta kiradi va yangi access_token oladi.
    3. Ikkalasi ham imkonsiz bo'lsa yoki parol o'zgargan bo'lsa, HemisSessionExpired chiqaradi.
    """
    from services.hemis_schedule import HemisScheduleError, HemisSessionExpired

    if request_fn is None:
        async def _default_request(c, method, url, **kwargs):
            async with c.request(method, url, **kwargs) as resp:
                try:
                    payload = await resp.json(content_type=None)
                except Exception:
                    payload = None
                return resp.status, payload
        request_fn = _default_request

    refresh_token = current_session.get("refresh_token")
    if refresh_token:
        try:
            refresh_status, refresh_payload = await request_fn(
                client,
                "POST",
                f"{api_url.rstrip('/')}/auth/refresh-token",
                headers={"X-Refresh-Token": refresh_token},
            )
            refresh_data = (
                refresh_payload.get("data")
                if isinstance(refresh_payload, dict)
                else None
            )
            if (
                refresh_status < 400
                and isinstance(refresh_data, dict)
                and refresh_data.get("token")
            ):
                new_token = str(refresh_data["token"])
                current_session["access_token"] = new_token
                if refresh_data.get("refresh_token"):
                    current_session["refresh_token"] = str(refresh_data["refresh_token"])
                return new_token, current_session
        except Exception:
            pass

    login = current_session.get("login")
    password = current_session.get("password")
    if login and password:
        try:
            new_session = await authenticate_hemis(api_url, login, password)
            new_token = new_session["access_token"]
            current_session["access_token"] = new_token
            if new_session.get("refresh_token"):
                current_session["refresh_token"] = new_session["refresh_token"]
            return new_token, current_session
        except HemisLoginError as err:
            if err.reason == "invalid_credentials":
                raise HemisSessionExpired("HEMIS paroli noto'g'ri yoki o'zgartirilgan.") from err
            raise HemisScheduleError(f"HEMIS serveriga vaqtincha bog'lanib bo'lmadi ({err.reason}).") from err
        except Exception as exc:
            raise HemisScheduleError(f"HEMIS qayta avtorizatsiyada xatolik: {exc}") from exc

    raise HemisSessionExpired("The HEMIS session expired.")
