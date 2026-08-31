"""
Management command: reconstruir_heads_grupo

Reconstrói os GroupIndividual com role=HEAD para grupos que têm GroupBeneficiary
activo mas cujo HEAD foi eliminado (soft-delete) — sem HEAD, o cálculo do payroll
GROUP falha com AttributeError: 'NoneType' object has no attribute 'individual'.

A correspondência é feita por: Group.code == Individual.json_ext['agregado_id']
(que é como o import_beneficiarios_excel cria ambos os registos).

Uso:
    # Ver o que seria reconstruído (sem guardar)
    python manage.py reconstruir_heads_grupo --dry-run

    # Executar
    python manage.py reconstruir_heads_grupo --username Admin

    # Apenas para um BenefitPlan específico
    python manage.py reconstruir_heads_grupo --benefit-plan-code PFV
"""
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Reconstrói GroupIndividual HEAD em falta para grupos com GroupBeneficiary activo'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', default=False,
                            help='Mostra o que seria feito sem guardar nada')
        parser.add_argument('--username', type=str, default='Admin',
                            help='Username openIMIS para auditoria (default: Admin)')
        parser.add_argument('--benefit-plan-code', type=str, default=None,
                            help='Filtrar por código de BenefitPlan (ex: PFV). '
                                 'Se omitido, processa todos os BenefitPlans GROUP.')

    def handle(self, *args, **options):
        from core.models import User
        from individual.models import Group, GroupIndividual, Individual
        from social_protection.models import GroupBeneficiary, BeneficiaryStatus, BenefitPlan

        dry_run = options['dry_run']
        username = options['username']
        bp_code = options['benefit_plan_code']

        # Verificar utilizador
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f"Utilizador não encontrado: {username}")

        if dry_run:
            self.stdout.write(self.style.WARNING("MODO DRY-RUN — nenhuma alteração será guardada"))
        self.stdout.write("")

        # ── 1. Encontrar GroupBeneficiary sem HEAD válido ──────────────────────
        gb_qs = GroupBeneficiary.objects.filter(
            status=BeneficiaryStatus.ACTIVE,
            is_deleted=False,
        ).select_related('group', 'benefit_plan')

        if bp_code:
            gb_qs = gb_qs.filter(benefit_plan__code=bp_code)
        else:
            # Apenas BenefitPlans GROUP
            gb_qs = gb_qs.filter(
                benefit_plan__type=BenefitPlan.BenefitPlanType.GROUP_TYPE
            )

        self.stdout.write(f"GroupBeneficiary activos a verificar: {gb_qs.count()}")
        self.stdout.write("")

        stats = {
            'verified': 0,
            'already_ok': 0,
            'rebuilt': 0,
            'no_individual': 0,
            'errors': [],
        }

        for gb in gb_qs.iterator(chunk_size=500):
            stats['verified'] += 1
            group = gb.group

            # Verificar se já tem HEAD válido
            head_exists = GroupIndividual.objects.filter(
                group=group,
                role=GroupIndividual.Role.HEAD,
                is_deleted=False,
                individual__isnull=False,
                individual__is_deleted=False,
            ).exists()

            if head_exists:
                stats['already_ok'] += 1
                continue

            # Encontrar o Individual correspondente por agregado_id == group.code
            individual = Individual.objects.filter(
                json_ext__agregado_id=group.code,
                is_deleted=False,
            ).first()

            if individual is None:
                msg = (f"Grupo {group.code} — sem Individual com "
                       f"json_ext.agregado_id='{group.code}' — ignorado")
                stats['no_individual'] += 1
                stats['errors'].append(msg)
                self.stdout.write(self.style.WARNING(f"  ⚠ {msg}"))
                continue

            # Criar o GroupIndividual HEAD
            try:
                with transaction.atomic():
                    if not dry_run:
                        gi = GroupIndividual(
                            group=group,
                            individual=individual,
                            role=GroupIndividual.Role.HEAD,
                            recipient_type=GroupIndividual.RecipientType.PRIMARY,
                        )
                        gi.save(username=username)

                stats['rebuilt'] += 1
                self.stdout.write(
                    f"  {'[DRY] ' if dry_run else ''}Grupo {group.code} → "
                    f"HEAD reconstruído: {individual.first_name} {individual.last_name}"
                )
            except Exception as exc:
                msg = f"Grupo {group.code} — ERRO ao criar HEAD: {exc}"
                stats['errors'].append(msg)
                self.stdout.write(self.style.ERROR(f"  ✖ {msg}"))
                logger.exception("[reconstruir_heads_grupo] %s", msg)

        # ── Resumo ───────────────────────────────────────────────────────────
        self.stdout.write("")
        self.stdout.write("=" * 60)
        self.stdout.write(f"Grupos verificados     : {stats['verified']}")
        self.stdout.write(f"Já tinham HEAD válido  : {stats['already_ok']}")
        self.stdout.write(f"HEADs reconstruídos    : {stats['rebuilt']}")
        self.stdout.write(f"Sem Individual match   : {stats['no_individual']}")

        if dry_run:
            self.stdout.write(self.style.WARNING(
                "\nDRY-RUN — execute sem --dry-run para aplicar as alterações"
            ))

        if stats['errors']:
            self.stdout.write(self.style.ERROR(f"\nERROS ({len(stats['errors'])}):"))
            for e in stats['errors']:
                self.stdout.write(self.style.ERROR(f"  {e}"))
        else:
            self.stdout.write(self.style.SUCCESS("\nSem erros."))
