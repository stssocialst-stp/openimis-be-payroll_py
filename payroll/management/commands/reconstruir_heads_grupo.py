"""
Management command: reconstruir_heads_grupo

Resolve o problema de GroupBeneficiary "órfãos" após substituição de beneficiários:

  Fase A — Soft-delete dos GroupBeneficiary antigos cujo grupo não tem HEAD activo
            (ficaram para trás após limpar_beneficiarios ou reimport de Individuals)

  Fase B — Criar GroupBeneficiary novos para os grupos que têm HEAD activo mas
            ainda não estão ligados ao BenefitPlan GROUP indicado

Uso:
    # Simulação (sem guardar)
    python manage.py reconstruir_heads_grupo --benefit-plan-code PFV --dry-run

    # Executar
    python manage.py reconstruir_heads_grupo --benefit-plan-code PFV --username Admin

Pré-requisito:
    O import_beneficiarios_excel já deve ter corrido e criado os novos
    Individual + Group + GroupIndividual (HEAD) no sistema.
"""
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = ('Limpa GroupBeneficiary órfãos e cria novos para os grupos actuais '
            'com HEAD válido no BenefitPlan GROUP indicado')

    def add_arguments(self, parser):
        parser.add_argument('--benefit-plan-code', type=str, required=True,
                            help='Código do BenefitPlan GROUP (ex: PFV)')
        parser.add_argument('--dry-run', action='store_true', default=False,
                            help='Mostra o que seria feito sem guardar nada')
        parser.add_argument('--username', type=str, default='Admin',
                            help='Username openIMIS para auditoria (default: Admin)')
        parser.add_argument('--skip-cleanup', action='store_true', default=False,
                            help='Ignorar Fase A (não apagar órfãos)')
        parser.add_argument('--skip-create', action='store_true', default=False,
                            help='Ignorar Fase B (não criar novos GroupBeneficiary)')

    def handle(self, *args, **options):
        from core.models import User
        from individual.models import Group, GroupIndividual
        from social_protection.models import (
            GroupBeneficiary, BeneficiaryStatus, BenefitPlan
        )

        dry_run = options['dry_run']
        username = options['username']
        bp_code = options['benefit_plan_code']

        # ── Validações iniciais ──────────────────────────────────────────────
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f"Utilizador não encontrado: {username}")

        try:
            benefit_plan = BenefitPlan.objects.get(
                code=bp_code,
                type=BenefitPlan.BenefitPlanType.GROUP_TYPE,
                is_deleted=False,
            )
        except BenefitPlan.DoesNotExist:
            raise CommandError(
                f"BenefitPlan GROUP com code='{bp_code}' não encontrado. "
                f"Confirma o código e que é do tipo GROUP."
            )

        if dry_run:
            self.stdout.write(self.style.WARNING("MODO DRY-RUN — nenhuma alteração será guardada\n"))

        self.stdout.write(f"BenefitPlan: {benefit_plan.code} — {benefit_plan.name}")
        self.stdout.write(f"Username   : {username}\n")

        stats = {
            'orphans_found': 0, 'orphans_deleted': 0,
            'groups_ok': 0, 'groups_created': 0,
            'errors': [],
        }

        # ════════════════════════════════════════════════════════════════
        # FASE A — Soft-delete dos GroupBeneficiary órfãos
        # (grupos sem GroupIndividual HEAD activo com Individual válido)
        # ════════════════════════════════════════════════════════════════
        if not options['skip_cleanup']:
            self.stdout.write("─" * 60)
            self.stdout.write("Fase A — Remover GroupBeneficiary órfãos")
            self.stdout.write("─" * 60)

            orphan_qs = GroupBeneficiary.objects.filter(
                benefit_plan=benefit_plan,
                status=BeneficiaryStatus.ACTIVE,
                is_deleted=False,
            ).select_related('group')

            for gb in orphan_qs.iterator(chunk_size=500):
                has_valid_head = GroupIndividual.objects.filter(
                    group=gb.group,
                    role=GroupIndividual.Role.HEAD,
                    is_deleted=False,
                    individual__isnull=False,
                    individual__is_deleted=False,
                ).exists()

                if has_valid_head:
                    continue  # não é órfão

                stats['orphans_found'] += 1
                self.stdout.write(
                    f"  {'[DRY] ' if dry_run else ''}Grupo {gb.group.code} "
                    f"— GroupBeneficiary sem HEAD → a remover"
                )
                try:
                    if not dry_run:
                        with transaction.atomic():
                            fresh = GroupBeneficiary.objects.get(id=gb.id)
                            fresh.delete(username=username)
                    stats['orphans_deleted'] += 1
                except Exception as exc:
                    msg = f"Grupo {gb.group.code} — ERRO ao remover: {exc}"
                    stats['errors'].append(msg)
                    self.stdout.write(self.style.ERROR(f"  ✖ {msg}"))

            self.stdout.write(
                f"\n  Órfãos encontrados : {stats['orphans_found']}"
            )
            self.stdout.write(
                f"  {'Seriam removidos' if dry_run else 'Removidos'} : {stats['orphans_deleted']}\n"
            )

        # ════════════════════════════════════════════════════════════════
        # FASE B — Criar GroupBeneficiary para grupos activos sem registo
        # (grupos com HEAD válido mas sem GroupBeneficiary para este plano)
        # ════════════════════════════════════════════════════════════════
        if not options['skip_create']:
            self.stdout.write("─" * 60)
            self.stdout.write("Fase B — Criar GroupBeneficiary para grupos activos")
            self.stdout.write("─" * 60)

            # Grupos que têm HEAD activo com Individual válido
            groups_with_head = Group.objects.filter(
                is_deleted=False,
                groupindividuals__role=GroupIndividual.Role.HEAD,
                groupindividuals__is_deleted=False,
                groupindividuals__individual__isnull=False,
                groupindividuals__individual__is_deleted=False,
            ).distinct()

            self.stdout.write(f"  Grupos com HEAD válido: {groups_with_head.count()}")

            for group in groups_with_head.iterator(chunk_size=500):
                # Verificar se já existe GroupBeneficiary activo para este plano
                already_exists = GroupBeneficiary.objects.filter(
                    group=group,
                    benefit_plan=benefit_plan,
                    is_deleted=False,
                ).exists()

                if already_exists:
                    stats['groups_ok'] += 1
                    continue

                # Criar
                stats['groups_created'] += 1
                self.stdout.write(
                    f"  {'[DRY] ' if dry_run else ''}Grupo {group.code} "
                    f"→ criar GroupBeneficiary"
                )
                try:
                    if not dry_run:
                        with transaction.atomic():
                            gb = GroupBeneficiary(
                                group=group,
                                benefit_plan=benefit_plan,
                                status=BeneficiaryStatus.ACTIVE,
                            )
                            gb.save(username=username)
                except Exception as exc:
                    msg = f"Grupo {group.code} — ERRO ao criar: {exc}"
                    stats['errors'].append(msg)
                    self.stdout.write(self.style.ERROR(f"  ✖ {msg}"))
                    stats['groups_created'] -= 1

            self.stdout.write(
                f"\n  Já tinham GroupBeneficiary : {stats['groups_ok']}"
            )
            self.stdout.write(
                f"  {'Seriam criados' if dry_run else 'Criados'} : {stats['groups_created']}\n"
            )

        # ── Resumo final ─────────────────────────────────────────────────────
        self.stdout.write("=" * 60)
        self.stdout.write(f"Fase A — Órfãos removidos    : {stats['orphans_deleted']}")
        self.stdout.write(f"Fase B — GroupBeneficiary criados: {stats['groups_created']}")

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
