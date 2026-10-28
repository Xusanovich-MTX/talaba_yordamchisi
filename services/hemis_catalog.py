"""Read and cache the official HEMIS institution directory."""

import time
from urllib.parse import urlsplit

import aiohttp


CATALOG_URL = "https://student.hemis.uz/rest/v1/public/university-api-urls"
CACHE_SECONDS = 3600
_cached_at = 0.0
_cached_institutions: list[dict[str, str]] = []


def _is_https_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme == "https" and bool(parsed.hostname)


async def get_hemis_institutions() -> list[dict[str, str]]:
    global _cached_at, _cached_institutions

    if _cached_institutions and time.monotonic() - _cached_at < CACHE_SECONDS:
        return _cached_institutions

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as client:
        async with client.get(CATALOG_URL) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)

    records = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ValueError("HEMIS institution directory has an invalid response.")

    institutions = []
    seen_codes = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        code = str(record.get("code", "")).strip()
        name = str(record.get("name", "")).strip()
        api_url = str(record.get("api_url", "")).strip()
        student_url = str(record.get("student_url", "")).strip()
        if (
            not code
            or code in seen_codes
            or not name
            or not _is_https_url(api_url)
            or not _is_https_url(student_url)
        ):
            continue
        seen_codes.add(code)
        institutions.append({
            "code": code,
            "name": name,
            "api_url": api_url,
            "student_url": student_url,
        })

    if not institutions:
        raise ValueError("HEMIS institution directory contains no usable institutions.")

    _cached_institutions = institutions
    _cached_at = time.monotonic()
    return _cached_institutions