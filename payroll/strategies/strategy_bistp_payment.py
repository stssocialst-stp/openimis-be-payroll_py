import logging
from datetime import date

from payroll.models import BenefitConsumptionStatus, PayrollStatus
from payroll.payment_gateway.bistp_gateway_connector import BistpGatewayConnector
from payroll.strategies.strategy_of_payments_interface import StrategyOfPaymentInterface
from payroll.strategies.strategy_online_payment import StrategyOnlinePayment

logger = logging.getLogger(__name__)


class StrategyBistpPayment(StrategyOfPaymentInterface):
    PAYMENT_GATEWAY = None

    @classmethod
    def initialize_payment_gateway(cls):
        logger.info("[BISTP][Strategy] A inicializar gateway BISTP")
        cls.PAYMENT_GATEWAY = BistpGatewayConnector()

    @classmethod
    def accept_payroll(cls, payroll, user, **kwargs):
        logger.info("[BISTP][Strategy] Payroll %s aprovado → APPROVE_FOR_PAYMENT (sem envio ao BISTP)", payroll.id)
        cls.change_status_of_payroll(payroll, PayrollStatus.APPROVE_FOR_PAYMENT, user)

    @classmethod
    def make_payment_for_payroll(cls, payroll, user, **kwargs):
        logger.info("[BISTP][Strategy] ====== INÍCIO DE PAGAMENTO — Payroll %s (%s) ======", payroll.id, payroll.name)

        if cls.PAYMENT_GATEWAY is None:
            cls.initialize_payment_gateway()

        benefits = StrategyOnlinePayment.get_benefits_attached_to_payroll(
            payroll, BenefitConsumptionStatus.ACCEPTED
        )

        batch_payments = []
        approved_benefits = []
        sem_nib = 0

        for benefit in benefits:
            nib = getattr(benefit.individual, 'nib', None)
            if not nib:
                sem_nib += 1
                logger.warning("[BISTP][Strategy] Benefício %s — individual %s SEM NIB — a ignorar",
                               benefit.code, benefit.individual_id)
                json_ext = benefit.json_ext or {}
                json_ext['bistp_skip_reason'] = 'nib_ausente'
                benefit.json_ext = json_ext
                benefit.save(username=user.username)
                continue

            batch_payments.append({
                "household_id": str(benefit.individual_id),
                "nib_number": nib,
                "amount": str(benefit.amount),
                "payment_date": date.today().isoformat(),
                "programme_name": "Cash Distribution",
            })
            approved_benefits.append(benefit)

        logger.info("[BISTP][Strategy] Payroll %s — válidos=%d, sem NIB=%d",
                    payroll.id, len(approved_benefits), sem_nib)

        if not batch_payments:
            logger.warning("[BISTP][Strategy] Payroll %s — sem pagamentos válidos para enviar", payroll.id)
            return

        ok = cls.PAYMENT_GATEWAY.send_payment_batch(str(payroll.id), batch_payments)

        if ok:
            StrategyOnlinePayment.approve_for_payment_benefit_consumption(approved_benefits, user)
            logger.info("[BISTP][Strategy] ====== FIM — Payroll %s — batch aceite, %d → APPROVE_FOR_PAYMENT ======",
                        payroll.id, len(approved_benefits))
        else:
            for benefit in approved_benefits:
                json_ext = benefit.json_ext or {}
                json_ext['bistp_skip_reason'] = 'batch_falhou'
                benefit.json_ext = json_ext
                benefit.save(username=user.username)
            logger.error("[BISTP][Strategy] ====== FIM — Payroll %s — batch rejeitado, %d mantidos em ACCEPTED ======",
                         payroll.id, len(approved_benefits))

    @classmethod
    def reconcile_payroll(cls, payroll, user):
        logger.info("[BISTP][Strategy] Payroll %s → RECONCILED", payroll.id)
        cls.change_status_of_payroll(payroll, PayrollStatus.RECONCILED, user)

    @classmethod
    def acknowledge_of_reponse_view(cls, payroll, response_from_gateway, user, rejected_bills):
        logger.debug("[BISTP][Strategy] acknowledge_of_reponse_view — payroll %s", payroll.id)
