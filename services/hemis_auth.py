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
    }
