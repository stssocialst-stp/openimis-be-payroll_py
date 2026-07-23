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
        total_benefits = len(list(benefits)) if hasattr(benefits, '__len__') else '?'
        logger.info("[BISTP][Strategy] Payroll %s — total de benefícios ACCEPTED a processar: %s",
                    payroll.id, total_benefits)

        # Re-fetch after len() consumed the queryset if it's a list
        benefits = StrategyOnlinePayment.get_benefits_attached_to_payroll(
            payroll, BenefitConsumptionStatus.ACCEPTED
        )

        approved = []
        sem_nib = 0
        falhou = 0
        processados = 0

        for benefit in benefits:
            processados += 1
            nib = getattr(benefit.individual, 'nib', None)

            if not nib:
                sem_nib += 1
                logger.warning(
                    "[BISTP][Strategy] [%d] Benefício %s — individual %s SEM NIB — a ignorar",
                    processados, benefit.code, benefit.individual_id
                )
                json_ext = benefit.json_ext or {}
                json_ext['bistp_skip_reason'] = 'nib_ausente'
                benefit.json_ext = json_ext
                benefit.save(username='bistp')
                continue

            nib_masked = f"{nib[:4]}***{nib[-2:]}" if len(nib) > 6 else nib
            logger.info(
                "[BISTP][Strategy] [%d] Benefício %s — individual %s, NIB=%s, amount=%s — a enviar",
                processados, benefit.code, benefit.individual_id, nib_masked, benefit.amount
            )

            ok = cls.PAYMENT_GATEWAY.send_payment(
                invoice_id=benefit.code,
                amount=str(benefit.amount),
                nib=nib,
                household_id=str(benefit.individual_id),
            )

            if ok:
                logger.info(
                    "[BISTP][Strategy] [%d] Benefício %s → ENVIADO COM SUCESSO",
                    processados, benefit.code
                )
                approved.append(benefit)
            else:
                falhou += 1
                logger.error(
                    "[BISTP][Strategy] [%d] Benefício %s → FALHOU envio — mantido em ACCEPTED",
                    processados, benefit.code
                )
                json_ext = benefit.json_ext or {}
                json_ext['bistp_skip_reason'] = 'envio_falhou'
                benefit.json_ext = json_ext
                benefit.save(username='bistp')

        StrategyOnlinePayment.approve_for_payment_benefit_consumption(approved, user)

        logger.info(
            "[BISTP][Strategy] ====== FIM DE PAGAMENTO — Payroll %s ======\n"
            "  Total processados : %d\n"
            "  Enviados OK       : %d\n"
            "  Sem NIB           : %d\n"
            "  Falhou envio      : %d",
            payroll.id, processados, len(approved), sem_nib, falhou
        )

        if falhou > 0:
            logger.warning(
                "[BISTP][Strategy] Payroll %s — %d benefício(s) falharam. "
                "Verificar logs [BISTP][Payment] acima para detalhe por invoice.",
                payroll.id, falhou
            )

    @classmethod
    def reconcile_payroll(cls, payroll, user):
        logger.info("[BISTP][Strategy] Payroll %s → RECONCILED", payroll.id)
        cls.change_status_of_payroll(payroll, PayrollStatus.RECONCILED, user)

    @classmethod
    def acknowledge_of_reponse_view(cls, payroll, response_from_gateway, user, rejected_bills):
        logger.debug("[BISTP][Strategy] acknowledge_of_reponse_view — payroll %s", payroll.id)
