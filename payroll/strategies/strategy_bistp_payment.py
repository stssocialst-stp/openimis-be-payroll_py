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
        import os as _os
        logger.info(
            "[BISTP][Strategy] ====== INÍCIO DE PAGAMENTO — Payroll %s (%s) ======\n"
            "  user:            %s\n"
            "  BISTP_BASE_URL:  %s\n"
            "  BISTP_PORT:      %s\n"
            "  BISTP_CLIENT_ID: %s\n"
            "  secret_defined:  %s",
            payroll.id, payroll.name,
            user.username if user else None,
            _os.environ.get('BISTP_BASE_URL', '<NÃO DEFINIDO>'),
            _os.environ.get('BISTP_PORT', '<NÃO DEFINIDO>'),
            _os.environ.get('BISTP_CLIENT_ID', '<NÃO DEFINIDO>'),
            bool(_os.environ.get('BISTP_CLIENT_SECRET', '')),
        )

        if cls.PAYMENT_GATEWAY is None:
            cls.initialize_payment_gateway()

        payment_date = payroll.payment_date
        if payment_date is None:
            payment_date = date.today()
            logger.warning("[BISTP][Strategy] Payroll %s sem payment_date — a usar a data de hoje (%s)",
                           payroll.id, payment_date)
        elif payment_date < date.today():
            logger.warning("[BISTP][Strategy] Payroll %s com payment_date no passado (%s)",
                           payroll.id, payment_date)

        # Ordem fixa: o mesmo conjunto de benefícios gera sempre os mesmos chunks (e transaction_id)
        benefits = StrategyOnlinePayment.get_benefits_attached_to_payroll(
            payroll, BenefitConsumptionStatus.ACCEPTED
        ).order_by('id')

        batch_payments = []
        approved_benefits = []
        sem_nib = 0

        for benefit in benefits:
            ind_json = benefit.individual.json_ext or {} if benefit.individual else {}
            nib = (
                ind_json.get('NIB') or
                ind_json.get('nib') or
                getattr(benefit.individual, 'nib', None)
            )
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
                "payment_date": payment_date.isoformat(),
                "currency": "STN",
                "programme_name": "Cash Distribution",
            })
            approved_benefits.append(benefit)

        logger.info("[BISTP][Strategy] Payroll %s — válidos=%d, sem NIB=%d",
                    payroll.id, len(approved_benefits), sem_nib)

        if not batch_payments:
            logger.warning("[BISTP][Strategy] Payroll %s — sem pagamentos válidos para enviar", payroll.id)
            return

        accepted_indexes, rejected = cls.PAYMENT_GATEWAY.send_payment_batch(str(payroll.id), batch_payments)
        accepted_indexes = set(accepted_indexes)
        # Pagamentos aceites já criaram ordens de pagamento no BISTP: têm de sair de ACCEPTED mesmo que
        # outros falhem, senão o próximo envio repete-os
        sent_benefits = [b for i, b in enumerate(approved_benefits) if i in accepted_indexes]
        rejected_benefits = [(b, rejected[i]) for i, b in enumerate(approved_benefits) if i in rejected]
        failed_benefits = [b for i, b in enumerate(approved_benefits)
                           if i not in accepted_indexes and i not in rejected]

        if sent_benefits:
            if payroll.payment_date is None:
                payroll.payment_date = payment_date
                payroll.save(username=user.username)
            for benefit in sent_benefits:
                benefit.payment_date = payment_date
            StrategyOnlinePayment.approve_for_payment_benefit_consumption(sent_benefits, user)

        # Recusados pelo BISTP (ex.: conta bloqueada/encerrada): mesmo tratamento do callback de recusa
        for benefit, reason in rejected_benefits:
            json_ext = benefit.json_ext or {}
            json_ext.pop('bistp_skip_reason', None)
            json_ext['bistp_rejection_reason'] = reason
            benefit.json_ext = json_ext
            benefit.status = BenefitConsumptionStatus.REJECTED
            try:
                benefit.save(username=user.username)
            except Exception:
                logger.warning("[BISTP][Strategy] Não foi possível marcar benefit %s como REJECTED", benefit.code)
            logger.warning("[BISTP][Strategy] Benefício %s (individual %s) recusado pelo BISTP: %s",
                           benefit.code, benefit.individual_id, reason)

        for benefit in failed_benefits:
            json_ext = benefit.json_ext or {}
            if json_ext.get('bistp_skip_reason') == 'batch_falhou':
                continue
            json_ext['bistp_skip_reason'] = 'batch_falhou'
            benefit.json_ext = json_ext
            try:
                benefit.save(username=user.username)
            except Exception:
                logger.warning("[BISTP][Strategy] Não foi possível guardar bistp_skip_reason para benefit %s",
                               benefit.code)

        log = logger.error if failed_benefits else logger.info
        log("[BISTP][Strategy] ====== FIM — Payroll %s — %d → APPROVE_FOR_PAYMENT, %d → REJECTED (recusados "
            "pelo BISTP), %d mantidos em ACCEPTED (chunks falhados) ======",
            payroll.id, len(sent_benefits), len(rejected_benefits), len(failed_benefits))

    @classmethod
    def reconcile_payroll(cls, payroll, user):
        logger.info("[BISTP][Strategy] Payroll %s → RECONCILED", payroll.id)
        cls.change_status_of_payroll(payroll, PayrollStatus.RECONCILED, user)

    @classmethod
    def acknowledge_of_reponse_view(cls, payroll, response_from_gateway, user, rejected_bills):
        logger.debug("[BISTP][Strategy] acknowledge_of_reponse_view — payroll %s", payroll.id)
