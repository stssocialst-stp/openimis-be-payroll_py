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
        self._base_url = os.environ['BISTP_BASE_URL']
        self._api_path = os.environ.get('BISTP_API_BASE_PATH', '/cxf/banco-mundial')
        self._ssl_verify = os.environ.get('BISTP_SSL_VERIFY', 'False').strip().lower() not in ('false', '0', '')
        self._timeout = int(os.environ.get('BISTP_TIMEOUT', '10'))

    def _auth_headers(self):
        return {'Authorization': f'Bearer {self._token_manager.get_token()}'}

    def send_payment(self, invoice_id, amount, nib=None, household_id=None, **kwargs):
        url = f"{self._base_url}{self._api_path}/api/payments/initiate"
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

        for attempt in range(3):
            try:
                response = requests.post(
                    url,
                    json=payload,
                    headers=self._auth_headers(),
                    verify=self._ssl_verify,
                    timeout=self._timeout,
                )
                if response.status_code == 200:
                    return response.json().get('status') == 'success'
                if response.status_code == 401:
                    self._token_manager.invalidate()
                    continue
                if response.status_code < 500:
                    logger.error("BISTP recusou pagamento %s: HTTP %s", invoice_id, response.status_code)
                    return False
                logger.warning("BISTP erro 5xx tentativa %d para %s", attempt + 1, invoice_id)
            except requests.Timeout:
                logger.warning("BISTP timeout tentativa %d para %s", attempt + 1, invoice_id)
            except Exception:
                logger.exception("BISTP erro inesperado para %s", invoice_id)
                return False
            time.sleep(2 ** attempt)

        logger.error("BISTP falhou após 3 tentativas para %s", invoice_id)
        return False

    def get_account_info(self, nib):
        url = f"{self._base_url}{self._api_path}/api/accounts/info"
        try:
            response = requests.get(
                url,
                params={'account_number': nib},
                headers=self._auth_headers(),
                verify=self._ssl_verify,
                timeout=self._timeout,
            )
            if response.status_code == 200:
                return response.json()
            if response.status_code == 401:
                self._token_manager.invalidate()
            logger.warning("BISTP get_account_info HTTP %s", response.status_code)
        except Exception:
            logger.exception("BISTP get_account_info falhou")
        return None

    def reconcile(self, invoice_id=None, amount=None, **kwargs):
        return True
