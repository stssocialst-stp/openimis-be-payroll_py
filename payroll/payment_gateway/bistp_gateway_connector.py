import logging
import os
import time
from datetime import date

import requests

from payroll.payment_gateway.bistp_token_manager import BistpTokenManager
from payroll.payment_gateway.payment_gateway_connector import PaymentGatewayConnector

logger = logging.getLogger(__name__)


class BistpGatewayConnector(PaymentGatewayConnector):

    def __init__(self):
        self._token_manager = BistpTokenManager()
        base_url = os.environ['BISTP_BASE_URL'].rstrip('/')
        port = os.environ.get('BISTP_PORT', '').strip()
        self._base_url = f"{base_url}:{port}" if port else base_url
        self._api_path = os.environ.get('BISTP_API_BASE_PATH', '/cxf/banco-mundial')
        self._ssl_verify = os.environ.get('BISTP_SSL_VERIFY', 'False').strip().lower() not in ('false', '0', '')
        self._timeout = int(os.environ.get('BISTP_TIMEOUT', '10'))
        logger.info("[BISTP][Connector] Inicializado — base_url=%s, ssl_verify=%s, timeout=%ds",
                    self._base_url, self._ssl_verify, self._timeout)

    def _auth_headers(self):
        return {'Authorization': f'Bearer {self._token_manager.get_token()}'}

    def send_payment(self, invoice_id, amount, nib=None, household_id=None, **kwargs):
        url = f"{self._base_url}{self._api_path}/api/payments/initiate"
        nib_masked = f"{nib[:4]}***{nib[-2:]}" if nib and len(nib) > 6 else nib
        payload = {
            "transaction_id": str(invoice_id),
            "payments": [{
                "household_id": str(household_id),
                "nib_number": nib,
                "amount": str(amount),
                "payment_date": date.today().isoformat(),
                "programme_name": "Cash Distribution",
            }],
        }

        logger.info("[BISTP][Payment] Iniciando envio — invoice=%s, individual=%s, nib=%s, amount=%s",
                    invoice_id, household_id, nib_masked, amount)

        for attempt in range(3):
            try:
                logger.debug("[BISTP][Payment] POST %s (tentativa %d/3)", url, attempt + 1)
                t0 = time.time()
                response = requests.post(
                    url,
                    json=payload,
                    headers=self._auth_headers(),
                    verify=self._ssl_verify,
                    timeout=self._timeout,
                )
                elapsed = time.time() - t0
                logger.info("[BISTP][Payment] invoice=%s → HTTP %s (%.3fs, tentativa %d/3)",
                            invoice_id, response.status_code, elapsed, attempt + 1)

                if response.status_code == 200:
                    body = response.json()
                    status = body.get('status')
                    logger.info("[BISTP][Payment] invoice=%s → status_resposta='%s' body=%s",
                                invoice_id, status, str(body)[:200])
                    success = status == 'success'
                    if not success:
                        logger.warning("[BISTP][Payment] invoice=%s aceite pelo servidor mas status='%s' (esperado 'success')",
                                       invoice_id, status)
                    return success

                if response.status_code == 401:
                    logger.warning("[BISTP][Payment] invoice=%s → 401 Unauthorized — a invalidar token e retry",
                                   invoice_id)
                    self._token_manager.invalidate()
                    continue

                if response.status_code < 500:
                    logger.error("[BISTP][Payment] invoice=%s → HTTP %s (erro de cliente, sem retry) body=%s",
                                 invoice_id, response.status_code, response.text[:300])
                    return False

                logger.warning("[BISTP][Payment] invoice=%s → HTTP %s (erro servidor, tentativa %d/3) body=%s",
                               invoice_id, response.status_code, attempt + 1, response.text[:200])

            except requests.Timeout:
                logger.warning("[BISTP][Payment] invoice=%s → Timeout (tentativa %d/3, timeout=%ds)",
                               invoice_id, attempt + 1, self._timeout)
            except requests.ConnectionError as exc:
                logger.error("[BISTP][Payment] invoice=%s → Erro de ligação: %s", invoice_id, exc)
                return False
            except Exception:
                logger.exception("[BISTP][Payment] invoice=%s → Erro inesperado", invoice_id)
                return False

            sleep_time = 2 ** attempt
            logger.debug("[BISTP][Payment] invoice=%s → aguardar %ds antes de retry", invoice_id, sleep_time)
            time.sleep(sleep_time)

        logger.error("[BISTP][Payment] invoice=%s → FALHOU após 3 tentativas", invoice_id)
        return False

    def send_payment_batch(self, batch_id, payments):
        url = f"{self._base_url}{self._api_path}/api/payments/initiate"
        payload = {
            "transaction_id": str(batch_id),
            "payments": payments,
        }
        logger.info("[BISTP][Batch] Iniciando batch — batch_id=%s, total=%d pagamentos", batch_id, len(payments))

        for attempt in range(3):
            try:
                t0 = time.time()
                response = requests.post(
                    url,
                    json=payload,
                    headers=self._auth_headers(),
                    verify=self._ssl_verify,
                    timeout=self._timeout,
                )
                elapsed = time.time() - t0
                logger.info("[BISTP][Batch] batch_id=%s → HTTP %s (%.3fs, tentativa %d/3)",
                            batch_id, response.status_code, elapsed, attempt + 1)

                if response.status_code == 200:
                    body = response.json()
                    success = body.get('status') == 'success'
                    if not success:
                        logger.warning("[BISTP][Batch] batch_id=%s aceite mas status='%s'",
                                       batch_id, body.get('status'))
                    return success

                if response.status_code == 401:
                    logger.warning("[BISTP][Batch] batch_id=%s → 401 — a invalidar token e retry", batch_id)
                    self._token_manager.invalidate()
                    continue

                if response.status_code < 500:
                    logger.error("[BISTP][Batch] batch_id=%s → HTTP %s (sem retry) body=%s",
                                 batch_id, response.status_code, response.text[:300])
                    return False

                logger.warning("[BISTP][Batch] batch_id=%s → HTTP %s (tentativa %d/3)",
                               batch_id, response.status_code, attempt + 1)

            except requests.Timeout:
                logger.warning("[BISTP][Batch] batch_id=%s → Timeout (tentativa %d/3)", batch_id, attempt + 1)
            except requests.ConnectionError as exc:
                logger.error("[BISTP][Batch] batch_id=%s → Erro de ligação: %s", batch_id, exc)
                return False
            except Exception:
                logger.exception("[BISTP][Batch] batch_id=%s → Erro inesperado", batch_id)
                return False

            time.sleep(2 ** attempt)

        logger.error("[BISTP][Batch] batch_id=%s → FALHOU após 3 tentativas", batch_id)
        return False

    def get_account_info(self, nib):
        url = f"{self._base_url}{self._api_path}/api/accounts/info"
        nib_masked = f"{nib[:4]}***{nib[-2:]}" if nib and len(nib) > 6 else nib
        logger.info("[BISTP][AccountInfo] GET info para NIB=%s", nib_masked)
        try:
            t0 = time.time()
            response = requests.get(
                url,
                params={'account_number': nib},
                headers=self._auth_headers(),
                verify=self._ssl_verify,
                timeout=self._timeout,
            )
            elapsed = time.time() - t0
            logger.info("[BISTP][AccountInfo] NIB=%s → HTTP %s (%.3fs)", nib_masked, response.status_code, elapsed)

            if response.status_code == 200:
                data = response.json()
                logger.debug("[BISTP][AccountInfo] NIB=%s → %s", nib_masked, str(data)[:200])
                return data

            if response.status_code == 401:
                logger.warning("[BISTP][AccountInfo] NIB=%s → 401 — a invalidar token", nib_masked)
                self._token_manager.invalidate()

            logger.warning("[BISTP][AccountInfo] NIB=%s → HTTP %s body=%s",
                           nib_masked, response.status_code, response.text[:200])
        except Exception:
            logger.exception("[BISTP][AccountInfo] NIB=%s → Erro inesperado", nib_masked)
        return None

    def reconcile(self, invoice_id=None, amount=None, **kwargs):
        logger.debug("[BISTP][Reconcile] invoice=%s amount=%s (operação local, sem chamada ao servidor)",
                     invoice_id, amount)
        return True
