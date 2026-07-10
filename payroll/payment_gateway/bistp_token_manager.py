import logging
import os
import time

import requests

logger = logging.getLogger(__name__)


class BistpTokenManager:
    _instance = None
    _token = None
    _expires_at = 0.0

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get_token(self):
        if self._token and time.time() < self._expires_at:
            return self._token
        return self._fetch_token()

    def _fetch_token(self):
        base_url = os.environ['BISTP_BASE_URL']
        endpoint = os.environ['BISTP_TOKEN_ENDPOINT']
        ssl_verify = os.environ.get('BISTP_SSL_VERIFY', 'False').strip().lower() not in ('false', '0', '')
        timeout = int(os.environ.get('BISTP_TIMEOUT', '10'))
        response = requests.post(
            f"{base_url}{endpoint}",
            data={
                'grant_type': 'client_credentials',
                'client_id': os.environ['BISTP_CLIENT_ID'],
                'client_secret': os.environ['BISTP_CLIENT_SECRET'],
            },
            verify=ssl_verify,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        self._token = data['access_token']
        self._expires_at = time.time() + data.get('expires_in', 3600) - 60
        logger.info("BISTP token renovado com sucesso")
        return self._token

    def invalidate(self):
        self._token = None
        self._expires_at = 0.0
