"""Small HTTP adapter, verified against official MAX OpenAPI 0.0.33."""

import asyncio
import ssl
from urllib.parse import urlsplit

import httpx


class APIError(Exception):
    def __init__(self, status=0, retry_after=None):
        super().__init__(f"MAX API: HTTP {status}")
        self.status = status
        self.retry_after = retry_after


class MaxAPI:
    def __init__(self, config, transport=None):
        self.cfg = config
        context = ssl.create_default_context()
        if config.max_ca_bundle:
            context.load_verify_locations(cafile=config.max_ca_bundle)
        self.client = httpx.AsyncClient(
            base_url=config.max_api_url,
            timeout=45,
            verify=context,
            transport=transport,
            headers={"Authorization": config.max_bot_token.get_secret_value()},
            follow_redirects=False,
        )
        # Never forward bot Authorization to the upload origin.
        self.upload_client = httpx.AsyncClient(
            timeout=120, verify=context, transport=transport, follow_redirects=False
        )
        self.gate = asyncio.Lock()

    async def close(self):
        await self.client.aclose()
        await self.upload_client.aclose()

    async def request(self, method, path, **kwargs):
        # Conservative per-process rate (<= 10 requests/s). 429 also persists a retry in outbox.
        async with self.gate:
            try:
                response = await self.client.request(method, path, **kwargs)
            except httpx.HTTPError:
                raise APIError() from None
            finally:
                await asyncio.sleep(0.11)
        if response.status_code >= 300:
            delay = response.headers.get("Retry-After", "")
            raise APIError(response.status_code, min(3600, max(1, int(delay))) if delay.isdigit() else None)
        try:
            return response.json()
        except ValueError:
            raise APIError(502) from None

    async def send_message(self, uid, payload):
        return await self.request("POST", "/messages", params={"user_id": uid}, json=payload)

    async def answer(self, payload):
        return await self.request(
            "POST",
            "/answers",
            params={"callback_id": payload["callback_id"]},
            json={"notification": payload["notification"]},
        )

    async def upload_file(self, data):
        endpoint = await self.request("POST", "/uploads", params={"type": "file"})
        url = endpoint.get("url", "")
        p = urlsplit(url)
        hosts = {h.strip().lower() for h in self.cfg.max_upload_allowed_hosts.split(",") if h.strip()}
        if (
            p.scheme != "https"
            or p.hostname not in hosts
            or p.username
            or p.password
            or p.port not in (None, 443)
        ):
            # No public fallback. Exact upload hosts must be permitted by operator and network policy.
            raise APIError(460)
        try:
            response = await self.upload_client.post(
                url, files={"data": ("rabochee-vremya.csv", data, "text/csv")}
            )
            if not 200 <= response.status_code < 300:
                raise APIError(response.status_code)
            result = response.json()
            if not isinstance(result.get("token"), str) or not result["token"]:
                raise APIError(502)
            return {"type": "file", "payload": {"token": result["token"]}}
        except (httpx.HTTPError, ValueError):
            raise APIError() from None
