"""
Management command: limpar_beneficiarios

Soft-delete de todos os beneficiários activos e suas ramificações, na ordem
correcta para preservar integridade referencial.

Ordem de eliminação:
  1. GroupIndividual  (dispara alignment service automaticamente)
  2. Beneficiary      (social_protection, se disponível)
  3. BenefitConsumption (apenas ACCEPTED e CREATED — nunca RECONCILED/APPROVE_FOR_PAYMENT)
  4. PayrollBenefitConsumption (ligados aos BC eliminados)
  5. Group            (apenas grupos sem membros activos)
  6. Individual

Uso:
    # Simular sem alterar nada
    python manage.py limpar_beneficiarios --dry-run

    # Executar (registos financeiros bloqueiam — use --force-skip-financial para confirmar)
    python manage.py limpar_beneficiarios --username admin

    # Executar reconhecendo que há registos financeiros que ficam intactos
    python manage.py limpar_beneficiarios --username admin --force-skip-financial
"""
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

logger = logging.getLogger(__name__)

STATUSES_PROTECTED = ['RECONCILED', 'APPROVE_FOR_PAYMENT']
STATUSES_DELETABLE = ['ACCEPTED', 'CREATED']


class _DryRunRollback(Exception):
    pass


class Command(BaseCommand):
    help = 'Soft-delete de todos os beneficiários activos e ramificações (Individual, Group, etc.)'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', default=False,
                            help='Simula sem guardar nada na base de dados')
        parser.add_argument('--username', type=str, default='admin',
                            help='Username openIMIS para auditoria (default: admin)')
        parser.add_argument('--force-skip-financial', action='store_true', default=False,
                            help='Prosseguir mesmo com registos financeiros protegidos (esses ficam intactos)')

    def handle(self, *args, **options):
        from individual.models import Individual, Group, GroupIndividual
        from payroll.models import BenefitConsumption, PayrollBenefitConsumption

        dry_run = options['dry_run']
        username = options['username']
        force_skip = options['force_skip_financial']

        self.stdout.write(f"[limpar] dry-run             : {dry_run}")
        self.stdout.write(f"[limpar] username             : {username}")
        self.stdout.write(f"[limpar] force-skip-financial: {force_skip}")
        self.stdout.write("")

        # ── Fase 0: Verificação de registos financeiros protegidos ──
        protected_count = BenefitConsumption.objects.filter(
            status__in=STATUSES_PROTECTED, is_deleted=False
        ).count()

        if protected_count > 0:
            msg = (
                f"Existem {protected_count} BenefitConsumption com status "
                f"RECONCILED ou APPROVE_FOR_PAYMENT. Estes registos NÃO podem ser eliminados.\n"
                f"  → Passe --force-skip-financial para prosseguir (esses registos ficam intactos).\n"
                f"  → Faça backup ANTES de qualquer limpeza: python manage.py backup_beneficiarios"
            )
            if not force_skip:
                raise CommandError(msg)
            self.stdout.write(self.style.WARNING(
                f"[limpar] AVISO: {protected_count} registos financeiros protegidos — serão ignorados"
            ))

        stats = {
            'gi_deleted': 0,
            'benef_deleted': 0,
            'bc_deleted': 0,
            'pbc_deleted': 0,
            'group_deleted': 0,
            'ind_deleted': 0,
            'errors': [],
        }

        # ── Fase 1: GroupIndividual ──
        self.stdout.write("[limpar] Fase 1: GroupIndividual...")
        gi_ids = list(GroupIndividual.objects.filter(is_deleted=False).values_list('id', flat=True))
        self.stdout.write(f"  → {len(gi_ids)} a processar")

        try:
            with transaction.atomic():
                for gi_id in gi_ids:
                    try:
                        fresh = GroupIndividual.objects.get(id=gi_id)
                        if fresh.is_deleted:
                            continue
                        fresh.delete(username=username)
                        stats['gi_deleted'] += 1
                    except Exception as exc:
                        msg = f"GroupIndividual {gi_id}: {exc}"
                        stats['errors'].append(msg)
                        logger.warning("[limpar] %s", msg)
                if dry_run:
                    raise _DryRunRollback()
        except _DryRunRollback:
            pass

        self.stdout.write(f"  ✓ {stats['gi_deleted']} eliminados" + (" [DRY-RUN]" if dry_run else ""))

        # ── Fase 2: Beneficiary ──
        try:
            from social_protection.models import Beneficiary
            self.stdout.write("[limpar] Fase 2: Beneficiary...")
            benef_ids = list(Beneficiary.objects.filter(is_deleted=False).values_list('id', flat=True))
            self.stdout.write(f"  → {len(benef_ids)} a processar")

            try:
                with transaction.atomic():
                    for b_id in benef_ids:
                        try:
                            fresh = Beneficiary.objects.get(id=b_id)
                            if fresh.is_deleted:
                                continue
                            fresh.delete(username=username)
                            stats['benef_deleted'] += 1
                        except Exception as exc:
                            msg = f"Beneficiary {b_id}: {exc}"
                            stats['errors'].append(msg)
                            logger.warning("[limpar] %s", msg)
                    if dry_run:
                        raise _DryRunRollback()
            except _DryRunRollback:
                pass

            self.stdout.write(f"  ✓ {stats['benef_deleted']} eliminados" + (" [DRY-RUN]" if dry_run else ""))
        except ImportError:
            self.stdout.write("[limpar] Fase 2: social_protection não disponível — ignorado")

        # ── Fase 3: BenefitConsumption (apenas ACCEPTED e CREATED) ──
        self.stdout.write("[limpar] Fase 3: BenefitConsumption (ACCEPTED + CREATED)...")
        bc_ids = list(
            BenefitConsumption.objects.filter(
                status__in=STATUSES_DELETABLE, is_deleted=False
            ).values_list('id', flat=True)
        )
        self.stdout.write(f"  → {len(bc_ids)} a processar")

        try:
            with transaction.atomic():
                for bc_id in bc_ids:
                    try:
                        fresh = BenefitConsumption.objects.get(id=bc_id)
                        if fresh.is_deleted:
                            continue

                        # Eliminar PayrollBenefitConsumption ligados primeiro
                        pbc_ids = list(
                            PayrollBenefitConsumption.objects.filter(
                                benefit_id=bc_id, is_deleted=False
                            ).values_list('id', flat=True)
                        )
                        for pbc_id in pbc_ids:
                            try:
                                pbc_fresh = PayrollBenefitConsumption.objects.get(id=pbc_id)
                                if not pbc_fresh.is_deleted:
                                    pbc_fresh.delete(username=username)
                                    stats['pbc_deleted'] += 1
                            except Exception as exc2:
                                msg = f"PayrollBenefitConsumption {pbc_id}: {exc2}"
                                stats['errors'].append(msg)
                                logger.warning("[limpar] %s", msg)

                        fresh.delete(username=username)
                        stats['bc_deleted'] += 1
                    except Exception as exc:
                        msg = f"BenefitConsumption {bc_id}: {exc}"
                        stats['errors'].append(msg)
                        logger.warning("[limpar] %s", msg)
                if dry_run:
                    raise _DryRunRollback()
        except _DryRunRollback:
            pass

        self.stdout.write(
            f"  ✓ {stats['bc_deleted']} BC + {stats['pbc_deleted']} PBC eliminados"
            + (" [DRY-RUN]" if dry_run else "")
        )

        # ── Fase 4: Group (apenas sem membros activos) ──
        self.stdout.write("[limpar] Fase 4: Group...")
        grp_ids = list(Group.objects.filter(is_deleted=False).values_list('id', flat=True))
        self.stdout.write(f"  → {len(grp_ids)} grupos a verificar")

        try:
            with transaction.atomic():
                for grp_id in grp_ids:
                    try:
                        has_members = GroupIndividual.objects.filter(
                            group_id=grp_id, is_deleted=False
                        ).exists()
                        if has_members:
                            continue
                        fresh = Group.objects.get(id=grp_id)
                        if fresh.is_deleted:
                            continue
                        fresh.delete(username=username)
                        stats['group_deleted'] += 1
                    except Exception as exc:
                        msg = f"Group {grp_id}: {exc}"
                        stats['errors'].append(msg)
                        logger.warning("[limpar] %s", msg)
                if dry_run:
                    raise _DryRunRollback()
        except _DryRunRollback:
            pass

        self.stdout.write(f"  ✓ {stats['group_deleted']} eliminados" + (" [DRY-RUN]" if dry_run else ""))

        # ── Fase 5: Individual ──
        self.stdout.write("[limpar] Fase 5: Individual...")
        ind_ids = list(Individual.objects.filter(is_deleted=False).values_list('id', flat=True))
        self.stdout.write(f"  → {len(ind_ids)} a processar")

        try:
            with transaction.atomic():
                for ind_id in ind_ids:
                    try:
                        fresh = Individual.objects.get(id=ind_id)
                        if fresh.is_deleted:
                            continue
                        fresh.delete(username=username)
                        stats['ind_deleted'] += 1
                    except Exception as exc:
                        msg = f"Individual {ind_id}: {exc}"
                        stats['errors'].append(msg)
                        logger.warning("[limpar] %s", msg)
                if dry_run:
                    raise _DryRunRollback()
        except _DryRunRollback:
            pass

        self.stdout.write(f"  ✓ {stats['ind_deleted']} eliminados" + (" [DRY-RUN]" if dry_run else ""))

        # ── Resumo ──
        self.stdout.write("")
        self.stdout.write("=" * 65)
        if dry_run:
            self.stdout.write(self.style.WARNING("MODO DRY-RUN — nenhuma alteração foi guardada"))
        if protected_count > 0:
            self.stdout.write(self.style.WARNING(
                f"Registos financeiros protegidos (intactos): {protected_count}"
            ))
        self.stdout.write(f"GroupIndividual eliminados  : {stats['gi_deleted']}")
        self.stdout.write(f"Beneficiary eliminados      : {stats['benef_deleted']}")
        self.stdout.write(f"BenefitConsumption eliminados: {stats['bc_deleted']}")
        self.stdout.write(f"PayrollBenefitConsumption   : {stats['pbc_deleted']}")
        self.stdout.write(f"Group eliminados             : {stats['group_deleted']}")
        self.stdout.write(f"Individual eliminados        : {stats['ind_deleted']}")
        if stats['errors']:
            self.stdout.write(self.style.ERROR(f"\nERROS ({len(stats['errors'])}):"))
            for e in stats['errors']:
                self.stdout.write(self.style.ERROR(f"  {e}"))
        else:
            self.stdout.write(self.style.SUCCESS("Sem erros."))
