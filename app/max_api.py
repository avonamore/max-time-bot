"""Verified MAX HTTP endpoints only. Token is never sent to upload hosts."""
import logging
import ssl
import time
from urllib.parse import urlsplit
import httpx

log = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status, retryable=False, retry_after=1):
        super().__init__(f'MAX API status={status}')
        self.status, self.retryable, self.retry_after = status, retryable, retry_after


class MaxAPI:
    def __init__(self, cfg, transport=None, sleep=time.sleep):
        self.cfg, self.sleep = cfg, sleep
        context = ssl.create_default_context()
        if cfg.max_ca_bundle:
            context.load_verify_locations(cafile=cfg.max_ca_bundle)
        # httpx exceptions may contain signed URLs. Never log their repr or traceback.
        self.client = httpx.Client(timeout=25, verify=context, transport=transport)
        self.last_request = 0.
        self.last_message = {}

    def close(self):
        self.client.close()

    def request(self, method, path, **kwargs):
        for attempt in range(5):
            self.sleep(max(0, 0.05 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = self.client.request(method, self.cfg.max_api_base + path,
                                               headers={'Authorization': self.cfg.max_bot_token}, **kwargs)
            except httpx.TransportError:
                if attempt == 4:
                    raise APIError(0, True) from None
                self.sleep(2 ** attempt)
                continue
            try:
                data = response.json()
            except ValueError:
                data = {}
            retry = response.status_code == 429 or response.status_code >= 500 or data.get('code') == 'attachment.not.ready'
            if response.is_success:
                return data
            delay = min(60, 2 ** attempt)
            try:
                delay = min(300, max(delay, float(response.headers.get('Retry-After', '0'))))
            except ValueError:
                pass
            if not retry or attempt == 4:
                raise APIError(response.status_code, retry, delay)
            self.sleep(delay)
        raise AssertionError('unreachable')

    def subscribe(self, url):
        if urlsplit(url).scheme != 'https':
            raise ValueError('Webhook должен использовать HTTPS')
        return self.request('POST', '/subscriptions', json={
            'url': url, 'update_types': ['bot_started', 'message_created', 'message_callback'],
            'secret': self.cfg.max_webhook_secret})

    def send(self, uid, body):
        self.sleep(max(0, 0.6 - (time.monotonic() - self.last_message.get(uid, 0))))
        self.last_message[uid] = time.monotonic()
        return self.request('POST', '/messages', params={'user_id': uid}, json=body)

    def answer(self, callback_id):
        return self.request('POST', '/answers', params={'callback_id': callback_id}, json={'notification': 'Обработано'})

    def upload_csv(self, data):
        info = self.request('POST', '/uploads', params={'type': 'file'})
        url = info.get('url', '')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.hostname not in self.cfg.max_upload_hosts.split(','):
            raise APIError(0, False)
        try:
            # A separate request without Authorization; signed URL authorizes upload.
            response = self.client.post(url, files={'data': ('report.csv', data, 'text/csv')})
            response.raise_for_status()
            token = response.json()['token']
            if not isinstance(token, str) or not token:
                raise ValueError()
        except (httpx.HTTPError, ValueError, KeyError):
            raise APIError(0, True) from None
        return token
