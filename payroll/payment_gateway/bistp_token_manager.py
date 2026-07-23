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
        remaining = self._expires_at - time.time()
        if self._token and remaining > 0:
            logger.debug("[BISTP][Token] Usando token em cache (expira em %.0fs)", remaining)
            return self._token
        logger.info("[BISTP][Token] Token expirado ou inexistente — a obter novo token")
        return self._fetch_token()

    def _fetch_token(self):
        base_url = os.environ['BISTP_BASE_URL']
        endpoint = os.environ['BISTP_TOKEN_ENDPOINT']
        ssl_verify = os.environ.get('BISTP_SSL_VERIFY', 'False').strip().lower() not in ('false', '0', '')
        timeout = int(os.environ.get('BISTP_TIMEOUT', '10'))
        client_id = os.environ['BISTP_CLIENT_ID']

        token_url = f"{base_url}{endpoint}"
        logger.info("[BISTP][Token] POST %s (client_id=%s, ssl_verify=%s, timeout=%ds)",
                    token_url, client_id, ssl_verify, timeout)

        try:
            response = requests.post(
                token_url,
                data={
                    'grant_type': 'client_credentials',
                    'client_id': client_id,
                    'client_secret': os.environ['BISTP_CLIENT_SECRET'],
                },
                verify=ssl_verify,
                timeout=timeout,
            )
            logger.info("[BISTP][Token] Resposta HTTP %s (%.3fs)",
                        response.status_code, response.elapsed.total_seconds())

            if response.status_code != 200:
                logger.error("[BISTP][Token] Falha ao obter token: HTTP %s — %s",
                             response.status_code, response.text[:300])
                response.raise_for_status()

            data = response.json()
            self._token = data['access_token']
            ttl = data.get('expires_in', 3600)
            self._expires_at = time.time() + ttl - 60
            logger.info("[BISTP][Token] Token obtido com sucesso (TTL=%ds, expira em %ds)",
                        ttl, ttl - 60)
            return self._token

        except requests.Timeout:
            logger.error("[BISTP][Token] Timeout ao obter token (url=%s, timeout=%ds)", token_url, timeout)
            raise
        except requests.ConnectionError as exc:
            logger.error("[BISTP][Token] Erro de ligação ao obter token: %s", exc)
            raise
        except Exception as exc:
            logger.exception("[BISTP][Token] Erro inesperado ao obter token: %s", exc)
            raise

    def invalidate(self):
        logger.warning("[BISTP][Token] Token invalidado manualmente (provável 401 do servidor)")
        self._token = None
        self._expires_at = 0.0
