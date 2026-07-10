import logging

from payroll.models import BenefitConsumptionStatus, PayrollStatus
from payroll.payment_gateway.bistp_gateway_connector import BistpGatewayConnector
from payroll.strategies.strategy_of_payments_interface import StrategyOfPaymentInterface
from payroll.strategies.strategy_online_payment import StrategyOnlinePayment

logger = logging.getLogger(__name__)


class StrategyBistpPayment(StrategyOfPaymentInterface):
    PAYMENT_GATEWAY = None

    @classmethod
    def initialize_payment_gateway(cls):
        cls.PAYMENT_GATEWAY = BistpGatewayConnector()

    @classmethod
    def accept_payroll(cls, payroll, user, **kwargs):
        cls.change_status_of_payroll(payroll, PayrollStatus.APPROVE_FOR_PAYMENT, user)

    @classmethod
    def make_payment_for_payroll(cls, payroll, user, **kwargs):
        if cls.PAYMENT_GATEWAY is None:
            cls.initialize_payment_gateway()

        benefits = StrategyOnlinePayment.get_benefits_attached_to_payroll(
            payroll, BenefitConsumptionStatus.ACCEPTED
        )
        approved = []
        sem_nib = 0
        falhou = 0
        for benefit in benefits:
            nib = getattr(benefit.individual, 'nib', None)
            if not nib:
                sem_nib += 1
                json_ext = benefit.json_ext or {}
                json_ext['bistp_skip_reason'] = 'nib_ausente'
                benefit.json_ext = json_ext
                benefit.save(username='bistp')
                logger.warning("Beneficiário %s sem NIB — ignorado", benefit.individual_id)
                continue
            ok = cls.PAYMENT_GATEWAY.send_payment(
                invoice_id=benefit.code,
                amount=str(benefit.amount),
                nib=nib,
                household_id=str(benefit.individual_id),
            )
            if ok:
                logger.info(
                    "BISTP benefit %s enviado (individual %s, NIB %s***)",
                    benefit.code, benefit.individual_id, nib[:4]
                )
                approved.append(benefit)
            else:
                falhou += 1
                json_ext = benefit.json_ext or {}
                json_ext['bistp_skip_reason'] = 'envio_falhou'
                benefit.json_ext = json_ext
                benefit.save(username='bistp')
                logger.warning("BISTP falhou a enviar benefit %s — mantido em ACCEPTED", benefit.code)

        StrategyOnlinePayment.approve_for_payment_benefit_consumption(approved, user)
        logger.info(
            "BISTP payroll %s: %d enviados, %d sem NIB, %d falhou",
            payroll.id, len(approved), sem_nib, falhou
        )

    @classmethod
    def reconcile_payroll(cls, payroll, user):
        cls.change_status_of_payroll(payroll, PayrollStatus.RECONCILED, user)

    @classmethod
    def acknowledge_of_reponse_view(cls, payroll, response_from_gateway, user, rejected_bills):
        pass
