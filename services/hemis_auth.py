"""Authenticate against the selected institution's official HEMIS API."""

import asyncio
from urllib.parse import urlsplit

import aiohttp


class HemisLoginError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def authenticate_hemis(
    api_url: str, login: str, password: str
) -> dict[str, str]:
    parsed = urlsplit(api_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HemisLoginError("unavailable")

    endpoint = f"{api_url.rstrip('/')}/auth/my-hemis-login"
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.post(
                endpoint,
                json={"login": login, "password": password},
            ) as response:
                payload = await response.json(content_type=None)
                status = response.status
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        raise HemisLoginError("unavailable") from None

    data = payload.get("data") if isinstance(payload, dict) else None
    error_code = data.get("error") if isinstance(data, dict) else None
    if status == 429:
        if error_code == "CAPTCHA_REQUIRED":
            raise HemisLoginError("captcha")
        raise HemisLoginError("rate_limited")
    if status >= 400:
        raise HemisLoginError("invalid_credentials" if status in (400, 401) else "unavailable")
    if not isinstance(data, dict) or not data.get("token"):
        raise HemisLoginError("invalid_credentials")

    return {
        "access_token": str(data["token"]),
        "refresh_token": str(data.get("refresh_token") or ""),
    }
