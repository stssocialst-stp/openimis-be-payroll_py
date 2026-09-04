import logging
import os
from io import StringIO

from celery import shared_task

from core.models import User
from payroll.models import Payroll, PayrollStatus, BenefitConsumptionStatus
from payroll.strategies import StrategyOnlinePayment
from payroll.payments_registry import PaymentMethodStorage

logger = logging.getLogger(__name__)

BACKUP_DIR = '/tmp/kenon_beneficiarios_backups'


def _update_mutation_log(client_mutation_id, status, error=None, json_ext=None):
    try:
        from core.models import MutationLog
        log = MutationLog.objects.filter(client_mutation_id=client_mutation_id).first()
        if not log:
            return
        log.status = status
        if error is not None:
            log.error = str(error)[:4000]
        if json_ext is not None:
            log.json_ext = json_ext
        log.save()
    except Exception as exc:
        logger.error(f"[MutationLog] update failed for {client_mutation_id}: {exc}")


@shared_task
def backup_beneficiarios_task(client_mutation_id, payroll_id, username):
    import glob as _glob
    from payroll.management.commands.backup_beneficiarios import Command
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        cmd = Command()
        cmd.stdout = StringIO()
        cmd.stderr = StringIO()
        cmd.handle(
            output_dir=BACKUP_DIR,
            filename_prefix='backup_beneficiarios',
            payroll_id=payroll_id,
            verbosity=1, no_color=False, force_color=False,
        )
        files = sorted(
            _glob.glob(os.path.join(BACKUP_DIR, 'backup_beneficiarios_*.json')),
            key=os.path.getmtime, reverse=True,
        )
        if not files:
            raise RuntimeError("Ficheiro de backup não gerado")
        _update_mutation_log(client_mutation_id, 2, json_ext={
            'backup_file': os.path.basename(files[0]),
        })
    except Exception as exc:
        logger.error(f"[backup_beneficiarios_task] {exc}")
        _update_mutation_log(client_mutation_id, 1, error=str(exc))


@shared_task
def limpar_beneficiarios_task(client_mutation_id, username, dry_run, force_skip_financial):
    from payroll.management.commands.limpar_beneficiarios import Command
    try:
        cmd = Command()
        out = StringIO()
        cmd.stdout = out
        cmd.stderr = StringIO()
        cmd.handle(
            username=username,
            dry_run=dry_run,
            force_skip_financial=force_skip_financial,
            verbosity=1, no_color=False, force_color=False,
        )
        _update_mutation_log(client_mutation_id, 2, json_ext={'output': out.getvalue()})
    except SystemExit as exc:
        _update_mutation_log(client_mutation_id, 1, error=str(exc))
    except Exception as exc:
        logger.error(f"[limpar_beneficiarios_task] {exc}")
        _update_mutation_log(client_mutation_id, 1, error=str(exc))


@shared_task
def importar_beneficiarios_task(client_mutation_id, tmp_excel_path, username,
                                 dry_run, sheet, benefit_plan_id, payroll_id, benefit_type):
    from payroll.management.commands.import_beneficiarios_excel import Command
    try:
        cmd = Command()
        out = StringIO()
        cmd.stdout = out
        cmd.stderr = StringIO()
        cmd.handle(
            excel_path=tmp_excel_path,
            username=username,
            dry_run=dry_run,
            benefit_type=benefit_type,
            sheet=sheet,
            benefit_plan_id=benefit_plan_id,
            payroll_id=payroll_id,
            verbosity=1, no_color=False, force_color=False,
        )
        _update_mutation_log(client_mutation_id, 2, json_ext={'output': out.getvalue()})
    except SystemExit as exc:
        _update_mutation_log(client_mutation_id, 1, error=str(exc))
    except Exception as exc:
        logger.error(f"[importar_beneficiarios_task] {exc}")
        _update_mutation_log(client_mutation_id, 1, error=str(exc))
    finally:
        try:
            os.unlink(tmp_excel_path)
        except Exception:
            pass


@shared_task
def restore_beneficiarios_task(client_mutation_id, tmp_backup_path, username, dry_run, skip_phases):
    from payroll.management.commands.restore_beneficiarios import Command
    try:
        cmd = Command()
        out = StringIO()
        cmd.stdout = out
        cmd.stderr = StringIO()
        cmd.handle(
            backup_file=tmp_backup_path,
            username=username,
            dry_run=dry_run,
            skip_phases=skip_phases,
            verbosity=1, no_color=False, force_color=False,
        )
        _update_mutation_log(client_mutation_id, 2, json_ext={'output': out.getvalue()})
    except SystemExit as exc:
        _update_mutation_log(client_mutation_id, 1, error=str(exc))
    except Exception as exc:
        logger.error(f"[restore_beneficiarios_task] {exc}")
        _update_mutation_log(client_mutation_id, 1, error=str(exc))
    finally:
        try:
            os.unlink(tmp_backup_path)
        except Exception:
            pass


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def generate_payroll_benefits_task(self, payroll_id, user_id):
    """
    Generates benefits for a payroll asynchronously.
    Called after the payroll header is saved so the HTTP request returns immediately.
    """
    try:
        from payroll.services import PayrollService
        payroll = Payroll.objects.get(id=payroll_id)
        user = User.objects.get(id=user_id)
        service = PayrollService(user)
        service._finalize_payroll_async(payroll)
        logger.info(f"[Payroll] Benefits generated for payroll {payroll_id}")
    except Exception as exc:
        logger.error(f"[Payroll] generate_payroll_benefits_task failed for {payroll_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task
def send_requests_to_gateway_payment(payroll_id, user_id):
    payroll = Payroll.objects.get(id=payroll_id)
    strategy = PaymentMethodStorage.get_chosen_payment_method(payroll.payment_method)
    if strategy:
        user = User.objects.get(id=user_id)
        strategy.initialize_payment_gateway()
        strategy.make_payment_for_payroll(payroll, user)


@shared_task
def send_request_to_reconcile(payroll_id, user_id):
    payroll = Payroll.objects.get(id=payroll_id)
    user = User.objects.get(id=user_id)
    strategy = StrategyOnlinePayment
    strategy.initialize_payment_gateway()
    strategy.change_status_of_payroll(payroll, PayrollStatus.RECONCILED, user)
    benefits = strategy.get_benefits_attached_to_payroll(payroll, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT)
    payment_gateway_connector = strategy.PAYMENT_GATEWAY
    benefits_to_reconcile = []
    for benefit in benefits:
        is_reconciled = payment_gateway_connector.reconcile(benefit.code, benefit.amount)
        # Initialize json_ext if it is None
        if benefit.json_ext is None:
            benefit.json_ext = {}
        if is_reconciled:
            new_json_ext = benefit.json_ext.copy() if benefit.json_ext else {}
            new_json_ext['output_gateway'] = is_reconciled
            new_json_ext['gateway_reconciliation_success'] = True
            benefit.json_ext = {**benefit.json_ext, **new_json_ext}
            benefits_to_reconcile.append(benefit)
        else:
            # Handle the case where a benefit payment is rejected
            new_json_ext = benefit.json_ext.copy() if benefit.json_ext else {}
            new_json_ext['output_gateway'] = is_reconciled
            new_json_ext['gateway_reconciliation_success'] = False
            benefit.json_ext = {**benefit.json_ext, **new_json_ext}
            benefit.save(username=user.login_name)
            logger.info(f"Payment for benefit ({benefit.code}) was rejected.")
    if benefits_to_reconcile:
        strategy.reconcile_benefit_consumption(benefits_to_reconcile, user)
