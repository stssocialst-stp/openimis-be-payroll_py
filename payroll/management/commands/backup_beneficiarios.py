"""
Management command: backup_beneficiarios

Exporta todos os dados activos de beneficiários para um ficheiro JSON com
timestamp, preservando UUIDs para uso posterior com restore_beneficiarios.

Uso:
    python manage.py backup_beneficiarios
    python manage.py backup_beneficiarios --output-dir /tmp
    python manage.py backup_beneficiarios --payroll-id <uuid>
"""
import json
import logging
import os
from datetime import datetime, date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


def _default_serial(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


class Command(BaseCommand):
    help = 'Exporta beneficiários activos para ficheiro JSON (backup)'

    def add_arguments(self, parser):
        parser.add_argument('--output-dir', type=str, default='.',
                            help='Directório de saída (default: directório actual)')
        parser.add_argument('--filename-prefix', type=str, default='backup_beneficiarios',
                            help='Prefixo do nome do ficheiro (default: backup_beneficiarios)')
        parser.add_argument('--payroll-id', type=str, default=None,
                            help='UUID do Payroll — filtra BenefitConsumption desse payroll')

    def handle(self, *args, **options):
        from individual.models import Individual, Group, GroupIndividual

        output_dir = Path(options['output_dir'])
        if not output_dir.exists():
            raise CommandError(f"Directório não existe: {output_dir}")

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        prefix = options['filename_prefix']
        output_file = output_dir / f"{prefix}_{timestamp}.json"

        payroll_id = options['payroll_id']

        self.stdout.write(f"[backup] A exportar para: {output_file}")

        backup = {
            'backup_timestamp': datetime.now().isoformat(),
            'backup_version': '1.0',
            'counts': {},
            'individuals': [],
            'groups': [],
            'group_individuals': [],
            'beneficiaries': [],
            'benefit_consumptions': [],
            'payroll_benefit_consumptions': [],
        }

        # ── Individuals ──
        self.stdout.write("[backup] A exportar Individuals...")
        ind_qs = Individual.objects.filter(is_deleted=False).select_related('location')
        for ind in ind_qs.iterator(chunk_size=500):
            backup['individuals'].append({
                'id': str(ind.id),
                'first_name': ind.first_name,
                'last_name': ind.last_name,
                'dob': ind.dob,
                'nib': getattr(ind, 'nib', None),
                'json_ext': ind.json_ext,
                'location_code': ind.location.code if ind.location else None,
                'location_id': str(ind.location_id) if ind.location_id else None,
                'version': ind.version,
            })
        self.stdout.write(f"  → {len(backup['individuals'])} individuals")

        # ── Groups ──
        self.stdout.write("[backup] A exportar Groups...")
        grp_qs = Group.objects.filter(is_deleted=False).select_related('location')
        for grp in grp_qs.iterator(chunk_size=500):
            backup['groups'].append({
                'id': str(grp.id),
                'code': grp.code,
                'json_ext': grp.json_ext,
                'location_code': grp.location.code if grp.location else None,
                'location_id': str(grp.location_id) if grp.location_id else None,
            })
        self.stdout.write(f"  → {len(backup['groups'])} groups")

        # ── GroupIndividuals ──
        self.stdout.write("[backup] A exportar GroupIndividuals...")
        gi_qs = GroupIndividual.objects.filter(is_deleted=False)
        for gi in gi_qs.iterator(chunk_size=500):
            backup['group_individuals'].append({
                'id': str(gi.id),
                'group_id': str(gi.group_id),
                'individual_id': str(gi.individual_id),
                'role': gi.role,
                'recipient_type': gi.recipient_type,
                'json_ext': gi.json_ext,
            })
        self.stdout.write(f"  → {len(backup['group_individuals'])} group_individuals")

        # ── Beneficiaries ──
        try:
            from social_protection.models import Beneficiary
            self.stdout.write("[backup] A exportar Beneficiaries...")
            benef_qs = Beneficiary.objects.filter(is_deleted=False)
            for b in benef_qs.iterator(chunk_size=500):
                backup['beneficiaries'].append({
                    'id': str(b.id),
                    'individual_id': str(b.individual_id),
                    'benefit_plan_id': str(b.benefit_plan_id),
                    'status': b.status,
                    'json_ext': b.json_ext,
                })
            self.stdout.write(f"  → {len(backup['beneficiaries'])} beneficiaries")
        except ImportError:
            self.stdout.write("[backup] Módulo social_protection não disponível — beneficiaries ignorados")

        # ── BenefitConsumptions ──
        try:
            from payroll.models import BenefitConsumption, PayrollBenefitConsumption
            self.stdout.write("[backup] A exportar BenefitConsumptions...")
            bc_qs = BenefitConsumption.objects.filter(is_deleted=False)
            if payroll_id:
                bc_qs = bc_qs.filter(payrollbenefitconsumption__payroll_id=payroll_id)
            for bc in bc_qs.iterator(chunk_size=500):
                backup['benefit_consumptions'].append({
                    'id': str(bc.id),
                    'individual_id': str(bc.individual_id) if bc.individual_id else None,
                    'code': bc.code,
                    'amount': str(bc.amount),
                    'type': bc.type,
                    'status': bc.status,
                    'date_due': bc.date_due,
                    'date_valid_from': bc.date_valid_from,
                    'date_valid_to': bc.date_valid_to,
                    'json_ext': bc.json_ext,
                })
            self.stdout.write(f"  → {len(backup['benefit_consumptions'])} benefit_consumptions")

            # ── PayrollBenefitConsumptions ──
            self.stdout.write("[backup] A exportar PayrollBenefitConsumptions...")
            pbc_qs = PayrollBenefitConsumption.objects.filter(is_deleted=False)
            if payroll_id:
                pbc_qs = pbc_qs.filter(payroll_id=payroll_id)
            for pbc in pbc_qs.iterator(chunk_size=500):
                backup['payroll_benefit_consumptions'].append({
                    'id': str(pbc.id),
                    'payroll_id': str(pbc.payroll_id),
                    'benefit_id': str(pbc.benefit_id),
                })
            self.stdout.write(f"  → {len(backup['payroll_benefit_consumptions'])} payroll_benefit_consumptions")
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"[backup] BenefitConsumption ignorado: {exc}"))

        # ── Counts ──
        backup['counts'] = {
            'individuals': len(backup['individuals']),
            'groups': len(backup['groups']),
            'group_individuals': len(backup['group_individuals']),
            'beneficiaries': len(backup['beneficiaries']),
            'benefit_consumptions': len(backup['benefit_consumptions']),
            'payroll_benefit_consumptions': len(backup['payroll_benefit_consumptions']),
        }

        # ── Gravar ficheiro ──
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(backup, f, ensure_ascii=False, indent=2, default=_default_serial)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"[backup] Concluído: {output_file}"))
        for k, v in backup['counts'].items():
            self.stdout.write(f"  {k:40s}: {v}")
