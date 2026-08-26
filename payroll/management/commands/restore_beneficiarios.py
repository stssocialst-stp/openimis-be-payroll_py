"""
Management command: restore_beneficiarios

Recria os dados de beneficiários a partir de um ficheiro JSON gerado por
backup_beneficiarios. Preserva os UUIDs originais para manter ligações
GenericFK (ex.: grievances ligadas a Individuals).

Uso:
    # Simular sem alterar nada
    python manage.py restore_beneficiarios backup_20260825_153000.json --dry-run

    # Executar restore completo
    python manage.py restore_beneficiarios backup_20260825_153000.json --username admin

    # Restaurar apenas indivíduos e grupos (saltar outras fases)
    python manage.py restore_beneficiarios backup.json --skip-phases beneficiaries consumptions
"""
import json
import logging
import uuid
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Model as DjangoModel
from django.utils import timezone

logger = logging.getLogger(__name__)

ALL_PHASES = ['individuals', 'groups', 'groupindividuals', 'beneficiaries', 'consumptions']


class _DryRunRollback(Exception):
    pass


class Command(BaseCommand):
    help = 'Restaura beneficiários a partir de ficheiro JSON de backup'

    def add_arguments(self, parser):
        parser.add_argument('backup_file', type=str,
                            help='Caminho para o ficheiro JSON de backup')
        parser.add_argument('--dry-run', action='store_true', default=False,
                            help='Simula sem guardar nada na base de dados')
        parser.add_argument('--username', type=str, default='admin',
                            help='Username openIMIS para auditoria (default: admin)')
        parser.add_argument('--skip-phases', nargs='*', default=[],
                            choices=ALL_PHASES,
                            help='Fases a ignorar: individuals groups groupindividuals beneficiaries consumptions')

    def handle(self, *args, **options):
        from core.models import User

        backup_file = Path(options['backup_file'])
        if not backup_file.exists():
            raise CommandError(f"Ficheiro não encontrado: {backup_file}")

        dry_run = options['dry_run']
        username = options['username']
        skip_phases = set(options['skip_phases'] or [])

        self.stdout.write(f"[restore] Ficheiro: {backup_file}")
        self.stdout.write(f"[restore] dry-run : {dry_run}")
        self.stdout.write(f"[restore] username: {username}")
        self.stdout.write(f"[restore] fases ignoradas: {skip_phases or 'nenhuma'}")
        self.stdout.write("")

        try:
            admin_user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f"Utilizador não encontrado: {username}")

        with open(backup_file, 'r', encoding='utf-8') as f:
            backup = json.load(f)

        self.stdout.write(f"[restore] Backup de: {backup.get('backup_timestamp', '?')}")
        counts = backup.get('counts', {})
        for k, v in counts.items():
            self.stdout.write(f"  {k:40s}: {v}")
        self.stdout.write("")

        stats = {
            'ind_created': 0, 'ind_skipped': 0,
            'grp_created': 0, 'grp_skipped': 0,
            'gi_created': 0, 'gi_skipped': 0,
            'benef_created': 0, 'benef_skipped': 0,
            'bc_created': 0, 'bc_skipped': 0,
            'pbc_created': 0, 'pbc_skipped': 0,
            'errors': [],
        }

        now = timezone.now()

        # ── Fase 1: Individuals ──
        if 'individuals' not in skip_phases:
            from individual.models import Individual
            self.stdout.write(f"[restore] Fase 1: Individuals ({len(backup.get('individuals', []))})...")
            try:
                with transaction.atomic():
                    for data in backup.get('individuals', []):
                        try:
                            ind_id = uuid.UUID(data['id'])
                            if Individual.objects.filter(id=ind_id).exists():
                                # Actualizar campos sem alterar UUID
                                Individual.objects.filter(id=ind_id).update(
                                    first_name=data['first_name'],
                                    last_name=data['last_name'],
                                    nib=data.get('nib') or '',
                                    json_ext=data.get('json_ext'),
                                    is_deleted=False,
                                    user_updated=admin_user,
                                    date_updated=now,
                                )
                                stats['ind_skipped'] += 1
                            else:
                                location_id = self._resolve_location_id(data)
                                ind = Individual(
                                    id=ind_id,
                                    first_name=data['first_name'],
                                    last_name=data.get('last_name', ''),
                                    nib=data.get('nib') or '',
                                    json_ext=data.get('json_ext'),
                                    location_id=location_id,
                                    is_deleted=False,
                                    version=1,
                                    user_created=admin_user,
                                    user_updated=admin_user,
                                    date_created=now,
                                    date_updated=now,
                                )
                                if data.get('dob'):
                                    ind.dob = self._parse_date(data['dob'])
                                DjangoModel.save(ind)  # preserva UUID, bypassa HistoryModel
                                stats['ind_created'] += 1
                        except Exception as exc:
                            msg = f"Individual {data.get('id', '?')}: {exc}"
                            stats['errors'].append(msg)
                            logger.warning("[restore] %s", msg)
                    if dry_run:
                        raise _DryRunRollback()
            except _DryRunRollback:
                pass
            self.stdout.write(
                f"  ✓ criados={stats['ind_created']} já-existiam={stats['ind_skipped']}"
                + (" [DRY-RUN]" if dry_run else "")
            )

        # ── Fase 2: Groups ──
        if 'groups' not in skip_phases:
            from individual.models import Group
            self.stdout.write(f"[restore] Fase 2: Groups ({len(backup.get('groups', []))})...")
            try:
                with transaction.atomic():
                    for data in backup.get('groups', []):
                        try:
                            grp_id = uuid.UUID(data['id'])
                            if Group.objects.filter(id=grp_id).exists():
                                Group.objects.filter(id=grp_id).update(
                                    code=data.get('code', ''),
                                    json_ext=data.get('json_ext'),
                                    is_deleted=False,
                                    user_updated=admin_user,
                                    date_updated=now,
                                )
                                stats['grp_skipped'] += 1
                            else:
                                location_id = self._resolve_location_id(data)
                                grp = Group(
                                    id=grp_id,
                                    code=data.get('code', ''),
                                    json_ext=data.get('json_ext'),
                                    location_id=location_id,
                                    is_deleted=False,
                                    version=1,
                                    user_created=admin_user,
                                    user_updated=admin_user,
                                    date_created=now,
                                    date_updated=now,
                                )
                                DjangoModel.save(grp)
                                stats['grp_created'] += 1
                        except Exception as exc:
                            msg = f"Group {data.get('id', '?')}: {exc}"
                            stats['errors'].append(msg)
                            logger.warning("[restore] %s", msg)
                    if dry_run:
                        raise _DryRunRollback()
            except _DryRunRollback:
                pass
            self.stdout.write(
                f"  ✓ criados={stats['grp_created']} já-existiam={stats['grp_skipped']}"
                + (" [DRY-RUN]" if dry_run else "")
            )

        # ── Fase 3: GroupIndividuals ──
        if 'groupindividuals' not in skip_phases:
            from individual.models import Group, GroupIndividual
            try:
                from individual.services import GroupAndGroupIndividualAlignmentService
                has_alignment = True
            except ImportError:
                has_alignment = False

            self.stdout.write(f"[restore] Fase 3: GroupIndividuals ({len(backup.get('group_individuals', []))})...")
            rebuilt_groups = set()
            try:
                with transaction.atomic():
                    for data in backup.get('group_individuals', []):
                        try:
                            gi_id = uuid.UUID(data['id'])
                            grp_id = uuid.UUID(data['group_id'])
                            ind_id = uuid.UUID(data['individual_id'])

                            if GroupIndividual.objects.filter(id=gi_id).exists():
                                GroupIndividual.objects.filter(id=gi_id).update(
                                    is_deleted=False,
                                    user_updated=admin_user,
                                    date_updated=now,
                                )
                                stats['gi_skipped'] += 1
                            else:
                                gi = GroupIndividual(
                                    id=gi_id,
                                    group_id=grp_id,
                                    individual_id=ind_id,
                                    role=data.get('role', GroupIndividual.Role.HEAD),
                                    recipient_type=data.get('recipient_type', GroupIndividual.RecipientType.PRIMARY),
                                    json_ext=data.get('json_ext'),
                                    is_deleted=False,
                                    version=1,
                                    user_created=admin_user,
                                    user_updated=admin_user,
                                    date_created=now,
                                    date_updated=now,
                                )
                                DjangoModel.save(gi)
                                stats['gi_created'] += 1
                            rebuilt_groups.add(grp_id)
                        except Exception as exc:
                            msg = f"GroupIndividual {data.get('id', '?')}: {exc}"
                            stats['errors'].append(msg)
                            logger.warning("[restore] %s", msg)

                    # Reconstruir json_ext dos Groups (members, head)
                    if has_alignment and not dry_run:
                        alignment_svc = GroupAndGroupIndividualAlignmentService(admin_user)
                        for grp_id in rebuilt_groups:
                            try:
                                grp = Group.objects.get(id=grp_id)
                                alignment_svc.update_json_ext_for_group(grp)
                            except Exception as exc:
                                logger.warning("[restore] Alignment falhou para Group %s: %s", grp_id, exc)

                    if dry_run:
                        raise _DryRunRollback()
            except _DryRunRollback:
                pass
            self.stdout.write(
                f"  ✓ criados={stats['gi_created']} já-existiam={stats['gi_skipped']}"
                + (" [DRY-RUN]" if dry_run else "")
            )

        # ── Fase 4: Beneficiaries ──
        if 'beneficiaries' not in skip_phases and backup.get('beneficiaries'):
            try:
                from social_protection.models import Beneficiary
                self.stdout.write(f"[restore] Fase 4: Beneficiaries ({len(backup['beneficiaries'])})...")
                try:
                    with transaction.atomic():
                        for data in backup['beneficiaries']:
                            try:
                                b_id = uuid.UUID(data['id'])
                                if Beneficiary.objects.filter(id=b_id).exists():
                                    Beneficiary.objects.filter(id=b_id).update(
                                        status=data.get('status', 'ACTIVE'),
                                        is_deleted=False,
                                        user_updated=admin_user,
                                        date_updated=now,
                                    )
                                    stats['benef_skipped'] += 1
                                else:
                                    b = Beneficiary(
                                        id=b_id,
                                        individual_id=uuid.UUID(data['individual_id']),
                                        benefit_plan_id=uuid.UUID(data['benefit_plan_id']),
                                        status=data.get('status', 'ACTIVE'),
                                        json_ext=data.get('json_ext'),
                                        is_deleted=False,
                                        version=1,
                                        user_created=admin_user,
                                        user_updated=admin_user,
                                        date_created=now,
                                        date_updated=now,
                                    )
                                    DjangoModel.save(b)
                                    stats['benef_created'] += 1
                            except Exception as exc:
                                msg = f"Beneficiary {data.get('id', '?')}: {exc}"
                                stats['errors'].append(msg)
                                logger.warning("[restore] %s", msg)
                        if dry_run:
                            raise _DryRunRollback()
                except _DryRunRollback:
                    pass
                self.stdout.write(
                    f"  ✓ criados={stats['benef_created']} já-existiam={stats['benef_skipped']}"
                    + (" [DRY-RUN]" if dry_run else "")
                )
            except ImportError:
                self.stdout.write("[restore] Fase 4: social_protection não disponível — ignorado")

        # ── Fase 5: BenefitConsumptions + PayrollBenefitConsumptions ──
        if 'consumptions' not in skip_phases and backup.get('benefit_consumptions'):
            from payroll.models import BenefitConsumption, PayrollBenefitConsumption
            self.stdout.write(f"[restore] Fase 5: BenefitConsumptions ({len(backup['benefit_consumptions'])})...")
            try:
                with transaction.atomic():
                    for data in backup['benefit_consumptions']:
                        try:
                            bc_id = uuid.UUID(data['id'])
                            if BenefitConsumption.objects.filter(id=bc_id).exists():
                                stats['bc_skipped'] += 1
                            else:
                                bc = BenefitConsumption(
                                    id=bc_id,
                                    individual_id=uuid.UUID(data['individual_id']) if data.get('individual_id') else None,
                                    code=data.get('code', ''),
                                    amount=Decimal(str(data.get('amount', '0'))),
                                    type=data.get('type', 'CASH'),
                                    status=data.get('status', 'ACCEPTED'),
                                    date_due=self._parse_date(data.get('date_due')),
                                    json_ext=data.get('json_ext'),
                                    is_deleted=False,
                                    version=1,
                                    user_created=admin_user,
                                    user_updated=admin_user,
                                    date_created=now,
                                    date_updated=now,
                                )
                                if data.get('date_valid_from'):
                                    bc.date_valid_from = self._parse_date(data['date_valid_from'])
                                DjangoModel.save(bc)
                                stats['bc_created'] += 1
                        except Exception as exc:
                            msg = f"BenefitConsumption {data.get('id', '?')}: {exc}"
                            stats['errors'].append(msg)
                            logger.warning("[restore] %s", msg)

                    for data in backup.get('payroll_benefit_consumptions', []):
                        try:
                            pbc_id = uuid.UUID(data['id'])
                            if PayrollBenefitConsumption.objects.filter(id=pbc_id).exists():
                                stats['pbc_skipped'] += 1
                            else:
                                pbc = PayrollBenefitConsumption(
                                    id=pbc_id,
                                    payroll_id=uuid.UUID(data['payroll_id']),
                                    benefit_id=uuid.UUID(data['benefit_id']),
                                    is_deleted=False,
                                    version=1,
                                    user_created=admin_user,
                                    user_updated=admin_user,
                                    date_created=now,
                                    date_updated=now,
                                )
                                DjangoModel.save(pbc)
                                stats['pbc_created'] += 1
                        except Exception as exc:
                            msg = f"PayrollBenefitConsumption {data.get('id', '?')}: {exc}"
                            stats['errors'].append(msg)
                            logger.warning("[restore] %s", msg)

                    if dry_run:
                        raise _DryRunRollback()
            except _DryRunRollback:
                pass
            self.stdout.write(
                f"  ✓ BC criados={stats['bc_created']} já-existiam={stats['bc_skipped']} "
                f"| PBC criados={stats['pbc_created']} já-existiam={stats['pbc_skipped']}"
                + (" [DRY-RUN]" if dry_run else "")
            )

        # ── Resumo ──
        self.stdout.write("")
        self.stdout.write("=" * 65)
        if dry_run:
            self.stdout.write(self.style.WARNING("MODO DRY-RUN — nenhuma alteração foi guardada"))
        self.stdout.write(f"Individuals  criados / já-existiam : {stats['ind_created']} / {stats['ind_skipped']}")
        self.stdout.write(f"Groups       criados / já-existiam : {stats['grp_created']} / {stats['grp_skipped']}")
        self.stdout.write(f"GrpIndiv.    criados / já-existiam : {stats['gi_created']} / {stats['gi_skipped']}")
        self.stdout.write(f"Beneficiary  criados / já-existiam : {stats['benef_created']} / {stats['benef_skipped']}")
        self.stdout.write(f"BenefitCons. criados / já-existiam : {stats['bc_created']} / {stats['bc_skipped']}")
        self.stdout.write(f"PayrollBC    criados / já-existiam : {stats['pbc_created']} / {stats['pbc_skipped']}")
        if stats['errors']:
            self.stdout.write(self.style.ERROR(f"\nERROS ({len(stats['errors'])}):"))
            for e in stats['errors']:
                self.stdout.write(self.style.ERROR(f"  {e}"))
        else:
            self.stdout.write(self.style.SUCCESS("Sem erros."))

    def _resolve_location_id(self, data):
        location_id = data.get('location_id')
        if location_id:
            try:
                from location.models import Location
                loc = Location.objects.filter(id=location_id).first()
                if loc:
                    return loc.id
            except Exception:
                pass
        location_code = data.get('location_code')
        if location_code:
            try:
                from location.models import Location
                loc = Location.objects.filter(code=location_code).first()
                if loc:
                    return loc.id
            except Exception:
                pass
        return None

    def _parse_date(self, raw):
        if raw is None:
            return None
        if isinstance(raw, (datetime, date)):
            return raw
        try:
            return datetime.fromisoformat(str(raw)).date()
        except Exception:
            return None
