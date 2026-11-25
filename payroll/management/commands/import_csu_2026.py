"""
Management command: import_csu_2026

Integra no Ké-Nón os agregados e os membros do CSU 2026 (exportação XLSX do KoboToolbox,
4 folhas), sem eliminar nem duplicar registos.

    Group            code = NIA validado (depois das correcções DPSSF)
    Individual       um por membro; json_ext['csu2026'] com as respostas do membro
    GroupIndividual  papel a partir do parentesco (F1) e do sexo

Os dados do agregado (localização, habitação, bens, emigrantes...) ficam no json_ext do chefe
em 'csu2026_agregado'. O módulo individual copia o json_ext do chefe para o do Group sempre que
um membro é gravado (GroupAndGroupIndividualAlignmentService.update_json_ext_for_group), por isso
uma chave própria do Group seria apagada.

Correspondência com o que já existe:
    Agregado  Group activo com code = NIA validado.
              Sem Group, mas com um Group no NIA original do CSU (que não foi atribuído a outro
              agregado deste ficheiro) → CONFLITO; o código não é alterado automaticamente.
    Membros   dentro do agregado: csu2026.member_key → BI → nome normalizado (sem BI contraditório).
              O chefe que já existe no Ké-Nón tem de corresponder a um membro do CSU; caso
              contrário → CONFLITO (NIA já usado por outro agregado ou chefe diferente).
              Membros do Ké-Nón que não estão no CSU ficam como estão.
    BI        BI repetido no ficheiro ou já usado noutro agregado: a pessoa é importada e
              sinalizada, sem fusão (decisão DPSSF de 23/11).

Ficam fora (decisão DPSSF de 23/11) os agregados não entrevistados e os presentes sem membros,
excepto os indicados como EXCECAO_SEM_MEMBROS no ficheiro de correcções (o titular é criado com
o nome do CSU e o sexo e a data de nascimento da lista de pagamento --lista).

Uso:
    python manage.py import_csu_2026 CSU.xlsx --correcoes correcoes.csv --lista lista.xlsx --dry-run
    python manage.py import_csu_2026 CSU.xlsx --correcoes correcoes.csv --lista lista.xlsx \\
        --username admin --relatorio relatorio_csu.csv
"""
import csv
import logging
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from payroll import csu2026
from payroll.csu2026 import normalize_key, normalize_name

logger = logging.getLogger(__name__)

PROGRESS_EVERY = 500
PLACEHOLDER_DOB = date(1900, 1, 1)


class _DryRunRollback(Exception):
    pass


class _HouseholdConflict(Exception):
    pass


class Command(BaseCommand):
    help = 'Importa os agregados e membros do CSU 2026 (XLSX do Kobo) para Group/Individual/GroupIndividual'

    def add_arguments(self, parser):
        parser.add_argument('excel_path', type=str, help='Ficheiro XLSX do CSU 2026 (4 folhas)')
        parser.add_argument('--correcoes', type=str, default=None,
                            help='CSV de correcções DPSSF (tipo,index,valor_anterior,valor_novo,referencia)')
        parser.add_argument('--lista', type=str, default=None,
                            help='Lista de pagamento (XLSX) — sexo e data de nascimento das excepções')
        parser.add_argument('--dry-run', action='store_true', default=False,
                            help='Simula tudo e desfaz no fim de cada agregado')
        parser.add_argument('--username', type=str, default='admin',
                            help='Username openIMIS para auditoria (default: admin)')
        parser.add_argument('--index', type=str, nargs='*', default=None,
                            help='Processar só estes INDEX do CSU')
        parser.add_argument('--limit', type=int, default=None,
                            help='Processar no máximo N agregados')
        parser.add_argument('--relatorio', type=str, default=None,
                            help='Exportar conflitos, sinalizações e exclusões para este CSV')

    # ────────────────────────────────────────────────────────
    # Execução
    # ────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        from core.models import User

        excel_path = Path(options['excel_path'])
        if not excel_path.exists():
            raise CommandError(f"Ficheiro não encontrado: {excel_path}")

        username = options['username']
        user = User.objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"Utilizador openIMIS não encontrado: {username}")
        dry_run = options['dry_run']

        try:
            corrections = csu2026.load_corrections(options['correcoes']) if options['correcoes'] else []
            self.stdout.write(f"[csu] A ler {excel_path} ...")
            csu_file = csu2026.read_csu_file(excel_path, corrections)
        except csu2026.CsuFileError as exc:
            raise CommandError(str(exc))

        households = list(csu_file.households.values())
        exceptions = [h for h in households if csu2026.is_no_members_exception(h)]
        list_data = self._read_payment_list(options['lista'], corrections) if exceptions else {}
        for household in exceptions:
            self._build_exception_head(household, list_data)

        if options['index']:
            wanted = {str(i) for i in options['index']}
            households = [h for h in households if h.index in wanted]
        if options['limit']:
            households = households[:options['limit']]

        self.stdout.write(f"[csu] dry-run   : {dry_run}")
        self.stdout.write(f"[csu] username  : {username}")
        self.stdout.write(f"[csu] agregados : {len(households)} | correcções: {len(corrections)}")
        self.stdout.write("")

        report = _Report(dry_run)
        report.orphan_member_data = csu_file.orphan_member_data
        context = _Context(
            user=user,
            locations=csu2026.build_district_cache(),
            final_nias={h.nia for h in csu_file.households.values()},
            bi_counts=Counter(m.bi for h in csu_file.households.values() for m in h.members if m.bi),
            existing_bis=self._existing_bis(),
        )

        for position, household in enumerate(households, start=1):
            reason = csu2026.exclusion_reason(household)
            if reason:
                report.excluded(household, reason)
                continue
            for correction in household.corrections:
                report.correction(household, correction)
            self._import_household(household, context, report, dry_run)
            if position % PROGRESS_EVERY == 0:
                self.stdout.write(f"[csu] {position}/{len(households)} agregados processados")

        report.print(self.stdout, self.style)
        if options['relatorio']:
            report.export(options['relatorio'])
            self.stdout.write(f"[csu] Relatório exportado para {options['relatorio']}")

    def _import_household(self, household, context, report, dry_run):
        # Contagens e sinalizações só entram no relatório se o agregado for até ao fim
        report.begin()
        try:
            with transaction.atomic():
                plan = self._plan_household(household, context)
                self._write_household(household, plan, context, report)
                report.household_done(household, plan)
                if dry_run:
                    raise _DryRunRollback()
        except _DryRunRollback:
            report.commit()
        except _HouseholdConflict as conflict:
            report.discard()
            report.conflict(household, str(conflict))
        except Exception as exc:
            report.discard()
            report.error(household, exc)
            logger.exception("[import_csu_2026] INDEX %s", household.index)
        else:
            report.commit()

    # ────────────────────────────────────────────────────────
    # Correspondência (sem escrever)
    # ────────────────────────────────────────────────────────

    def _plan_household(self, household, context):
        from individual.models import Group, GroupIndividual

        groups = list(Group.objects.filter(code=household.nia, is_deleted=False)[:2])
        if len(groups) > 1:
            raise _HouseholdConflict(f"existe mais do que um agregado activo com NIA {household.nia}")
        group = groups[0] if groups else None

        if group is None:
            original = household.nia_original
            if (original and original != household.nia and original not in context.final_nias
                    and Group.objects.filter(code=original, is_deleted=False).exists()):
                raise _HouseholdConflict(
                    f"NIA corrigido: existe no Ké-Nón o agregado {original} (NIA original do CSU) e não "
                    f"existe {household.nia}; o código não é alterado automaticamente"
                )

        existing = []
        if group is not None:
            existing = list(
                GroupIndividual.objects.filter(group=group, is_deleted=False).select_related('individual')
            )

        matches = self._match_members(household, existing)

        heads = [gi for gi in existing if gi.role == GroupIndividual.Role.HEAD]
        matched_ids = {ind.id for ind in matches.values() if ind is not None}
        for head_gi in heads:
            if head_gi.individual_id not in matched_ids:
                head = head_gi.individual
                kind = 'NIA novo já existe no Ké-Nón' if not household.nia_original else 'chefe diferente'
                raise _HouseholdConflict(
                    f"{kind}: o chefe no Ké-Nón ({head.first_name} {head.last_name}, {head.id}) "
                    f"não consta da composição do agregado no CSU"
                )

        # O chefe no Ké-Nón corresponde a outro membro do CSU: o chefe passa a ser o do CSU
        csu_head = matches.get(household.head.key)
        head_changed = bool(heads) and (csu_head is None or csu_head.id != heads[0].individual_id)
        return _Plan(group=group, existing=existing, matches=matches, head_changed=head_changed)

    def _match_members(self, household, existing):
        """member.key → Individual existente no agregado (ou None)."""
        individuals = [gi.individual for gi in existing if not gi.individual.is_deleted]
        by_key = {}
        by_bi = defaultdict(list)
        by_name = defaultdict(list)
        for individual in individuals:
            json_ext = individual.json_ext or {}
            key = (json_ext.get('csu2026') or {}).get('member_key')
            if key:
                by_key[key] = individual
            bi = csu2026.as_text(json_ext.get('bilhete_didentidade'))
            if bi:
                by_bi[bi].append(individual)
            by_name[normalize_name(f"{individual.first_name} {individual.last_name}")].append(individual)

        matches = {}
        used = set()

        def take(candidates):
            for candidate in candidates:
                if candidate.id not in used:
                    used.add(candidate.id)
                    return candidate
            return None

        # 1.ª passagem: chave do Kobo e BI; 2.ª: nome, só para quem ainda não tem correspondência
        for member in household.members:
            found = by_key.get(member.key)
            if found is not None and found.id not in used:
                used.add(found.id)
            else:
                found = take(by_bi.get(member.bi, [])) if member.bi else None
            matches[member.key] = found

        for member in household.members:
            if matches[member.key] is not None:
                continue
            candidates = []
            for individual in by_name.get(normalize_name(member.full_name), []):
                stored_bi = csu2026.as_text((individual.json_ext or {}).get('bilhete_didentidade'))
                if not (stored_bi and member.bi and stored_bi != member.bi):
                    candidates.append(individual)
            matches[member.key] = take(candidates)
        return matches

    # ────────────────────────────────────────────────────────
    # Escrita
    # ────────────────────────────────────────────────────────

    def _write_household(self, household, plan, context, report):
        from individual.models import Group, GroupIndividual
        from individual.services import GroupAndGroupIndividualAlignmentService

        user = context.user
        location = context.locations.get(normalize_key(household.district))
        if location is None:
            report.flag(household, 'distrito', f"distrito '{household.district}' sem Location no sistema")

        group = plan.group
        if group is None:
            group = Group(code=household.nia, location=location)
            group.save(user=user)
            plan.group_created = True
        else:
            if location is not None and group.location_id != location.id:
                group.location = location
                group.save(user=user)
            location = group.location

        gi_by_individual = {gi.individual_id: gi for gi in plan.existing}

        # Chefe antigo que passa a outro papel: muda-se primeiro, para o serviço do grupo
        # não ter de despromover o chefe (evita dois HEAD)
        if plan.head_changed:
            for member in household.members:
                individual = plan.matches.get(member.key)
                gi = gi_by_individual.get(individual.id) if individual else None
                if gi is not None and gi.role == GroupIndividual.Role.HEAD and not member.is_head:
                    gi.role = member.role
                    gi.save(user=user)
            report.flag(household, 'chefe alterado',
                        'o chefe no Ké-Nón era outro membro do agregado; passa a ser o chefe do CSU')

        has_primary = any(gi.recipient_type == GroupIndividual.RecipientType.PRIMARY for gi in plan.existing)

        for member in household.members:
            individual = plan.matches.get(member.key)
            created = individual is None
            individual = self._save_individual(household, member, individual, location, context, report)
            report.member(created, member)

            gi = gi_by_individual.get(individual.id)
            if gi is None:
                gi = GroupIndividual(group=group, individual=individual, role=member.role)
                if member.is_head and not has_primary:
                    gi.recipient_type = GroupIndividual.RecipientType.PRIMARY
                    has_primary = True
                gi.save(user=user)
                gi_by_individual[individual.id] = gi
            elif gi.role != member.role:
                gi.role = member.role
                gi.save(user=user)

        kept = [gi for gi in plan.existing
                if gi.individual_id not in {ind.id for ind in plan.matches.values() if ind}]
        report.kenon_only(len(kept))

        # Alinhar head/members/primary e copiar o json_ext do chefe (com csu2026_agregado) para o Group
        group = Group.objects.get(id=group.id)
        GroupAndGroupIndividualAlignmentService(user).update_json_ext_for_group(group)

    def _save_individual(self, household, member, individual, location, context, report):
        from individual.models import Individual

        user = context.user
        created = individual is None
        if created:
            individual = Individual(first_name=member.first_name, last_name=member.last_name, dob=member.dob)

        individual.first_name = member.first_name
        individual.last_name = member.last_name
        # Data estimada não substitui uma data verdadeira já registada
        if not member.dob_estimated or created or not individual.dob or individual.dob == PLACEHOLDER_DOB:
            individual.dob = member.dob
            dob_estimated = member.dob_estimated
        else:
            dob_estimated = False
        individual.location = location

        json_ext = dict(individual.json_ext or {})
        json_ext['agregado_id'] = household.nia
        if member.sex:
            json_ext['sexo'] = member.sex
        if member.bi:
            previous_bi = csu2026.as_text(json_ext.get('bilhete_didentidade'))
            if previous_bi and previous_bi != member.bi:
                report.flag(household, 'BI actualizado',
                            f"{member.full_name}: BI {previous_bi} → {member.bi} (correspondência pelo nome)")
            json_ext['bilhete_didentidade'] = member.bi
        if member.document_number:
            json_ext['documento_identidade'] = {'tipo': member.document_type, 'numero': member.document_number}
        if member.is_head:
            json_ext['index_csu2026'] = household.index
            json_ext['csu2026_agregado'] = household.data
        else:
            json_ext.pop('csu2026_agregado', None)
        json_ext['csu2026'] = {
            'member_key': member.key,
            'index': household.index,
            'pos': member.pos,
            'parentesco': member.relation,
            'dob_estimada': dob_estimated,
            'respostas': member.answers,
        }
        individual.json_ext = json_ext

        if created or individual.is_dirty(check_relationship=True):
            individual.save(user=user)

        if dob_estimated:
            report.flag(household, 'data estimada', f"{member.full_name}: {member.dob.isoformat()}", count_only=True)
        if member.bi:
            self._flag_repeated_bi(household, member, individual, context, report)
        return individual

    def _flag_repeated_bi(self, household, member, individual, context, report):
        count = context.bi_counts.get(member.bi, 0)
        if count > 1:
            report.flag(household, 'BI repetido', f"{member.full_name}: BI {member.bi} aparece em {count} membros do CSU")
            return
        others = [(ind_id, nia) for ind_id, nia in context.existing_bis.get(member.bi, [])
                  if ind_id != individual.id and nia != household.nia]
        if others:
            nias = ', '.join(sorted({nia or '?' for _, nia in others}))
            report.flag(household, 'BI repetido',
                        f"{member.full_name}: BI {member.bi} já existe no Ké-Nón noutro agregado ({nias})")

    def _existing_bis(self):
        """BI → [(id do Individual, NIA)] dos registos existentes antes da importação."""
        from individual.models import Individual

        bis = defaultdict(list)
        rows = Individual.objects.filter(is_deleted=False).values_list(
            'id', 'json_ext__bilhete_didentidade', 'json_ext__agregado_id')
        for ind_id, bi, nia in rows.iterator():
            bi = csu2026.as_text(bi)
            if bi:
                bis[bi].append((ind_id, csu2026.as_text(nia)))
        return bis

    # ────────────────────────────────────────────────────────
    # Excepções e lista de pagamento
    # ────────────────────────────────────────────────────────

    def _read_payment_list(self, path, corrections):
        import openpyxl

        if not path:
            raise CommandError("Há excepções EXCECAO_SEM_MEMBROS: indique a lista de pagamento com --lista")
        index_fixes = csu2026.list_index_corrections(corrections)
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            rows = workbook.worksheets[0].iter_rows(values_only=True)
            headers = [str(h).strip() if h is not None else '' for h in next(rows, [])]
            data = {}
            for row in rows:
                values = dict(zip(headers, row))
                index = csu2026.as_text(values.get('INDEX CSU2026'))
                if index in index_fixes:
                    index = index_fixes[index].new_value
                if index:
                    data[index] = values
        finally:
            workbook.close()
        return data

    def _build_exception_head(self, household, list_data):
        row = list_data.get(household.index)
        if row is None:
            raise CommandError(f"Excepção INDEX {household.index}: não consta da lista de pagamento")
        dob = csu2026.to_date(row.get('data_n'))
        if dob is None:
            raise CommandError(f"Excepção INDEX {household.index}: data de nascimento em falta na lista")
        name = household.respondent_name or csu2026.as_text(row.get('nome_chefe'))
        first_name, last_name = csu2026.split_name(name)
        household.members = [csu2026.Member(
            key=f"{household.uuid}:1",
            pos='1',
            full_name=name,
            first_name=first_name,
            last_name=last_name,
            sex=csu2026.sex_code(row.get('sexo_chefe')),
            dob=dob,
            dob_estimated=False,
            relation='CHEFE do agregado famíliar',
            role='HEAD',
            bi=csu2026.as_text(row.get('bilhete_didentidade')),
            document_type='',
            document_number='',
            answers={'origem': 'lista de pagamento (excepção DPSSF)'},
        )]


class _Context:
    def __init__(self, *, user, locations, final_nias, bi_counts, existing_bis):
        self.user = user
        self.existing_bis = existing_bis
        self.locations = locations
        self.final_nias = final_nias
        self.bi_counts = bi_counts


class _Plan:
    def __init__(self, *, group, existing, matches, head_changed):
        self.group = group
        self.existing = existing
        self.matches = matches
        self.head_changed = head_changed
        self.group_created = False


class _Report:
    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.counts = Counter()     # agregados/membros criados e actualizados, membros só no Ké-Nón
        self.flag_counts = Counter()
        self.rows = []              # (secção, INDEX, NIA, detalhe)
        self.orphan_member_data = 0
        self.conflicts = 0
        self.errors = 0
        self.excluded_by_reason = Counter()
        self._pending = None

    # Alterações do agregado em curso
    def begin(self):
        self._pending = (Counter(), Counter(), [])

    def commit(self):
        counts, flags, rows = self._pending
        self.counts.update(counts)
        self.flag_counts.update(flags)
        self.rows.extend(rows)
        self._pending = None

    def discard(self):
        self._pending = None

    def _target(self):
        return self._pending or (self.counts, self.flag_counts, self.rows)

    def _row(self, section, household, detail, pending=True):
        rows = self._target()[2] if pending else self.rows
        rows.append((section, household.index, household.nia, detail))

    def household_done(self, household, plan):
        self._target()[0]['groups_created' if plan.group_created else 'groups_updated'] += 1

    def member(self, created, member):
        self._target()[0]['members_created' if created else 'members_updated'] += 1

    def kenon_only(self, count):
        self._target()[0]['kenon_only_members'] += count

    def flag(self, household, kind, detail, count_only=False):
        self._target()[1][kind] += 1
        if not count_only:
            self._row(f"SINALIZADO: {kind}", household, detail)

    def correction(self, household, correction):
        self.flag_counts['correcção DPSSF'] += 1
        self._row('CORRECÇÃO DPSSF', household, correction.describe(), pending=False)

    def conflict(self, household, detail):
        self.conflicts += 1
        self._row('CONFLITO', household, detail, pending=False)

    def error(self, household, exc):
        self.errors += 1
        self._row('ERRO', household, str(exc), pending=False)

    def excluded(self, household, reason):
        self.excluded_by_reason[reason] += 1
        self._row('EXCLUÍDO', household, reason, pending=False)

    def print(self, out, style):
        out.write("")
        out.write("=" * 70)
        out.write(f"Agregados criados              : {self.counts['groups_created']}")
        out.write(f"Agregados actualizados         : {self.counts['groups_updated']}")
        out.write(f"Agregados em conflito          : {self.conflicts}")
        for reason, count in sorted(self.excluded_by_reason.items()):
            out.write(f"Agregados excluídos ({reason}) : {count}")
        out.write(f"Membros criados                : {self.counts['members_created']}")
        out.write(f"Membros actualizados           : {self.counts['members_updated']}")
        out.write(f"Membros só no Ké-Nón (mantidos): {self.counts['kenon_only_members']}")
        for kind, count in sorted(self.flag_counts.items()):
            out.write(f"Sinalizados — {kind:<17}: {count}")
        out.write(f"Linhas de outros_dados sem membro (ignoradas): {self.orphan_member_data}")
        out.write(f"Erros                          : {self.errors}")
        if self.dry_run:
            out.write(style.WARNING("MODO DRY-RUN — nenhuma alteração foi guardada"))

        by_section = defaultdict(list)
        for section, index, nia, detail in self.rows:
            if section != 'EXCLUÍDO':
                by_section[section].append(f"INDEX {index} (NIA {nia}) — {detail}")
        for section in sorted(by_section):
            items = by_section[section]
            painter = style.ERROR if section in ('CONFLITO', 'ERRO') else style.WARNING
            out.write(painter(f"\n{section} ({len(items)}):"))
            for item in items:
                out.write(painter(f"  {item}"))

    def export(self, path):
        with open(path, 'w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle, delimiter=';')
            writer.writerow(['secção', 'INDEX', 'NIA', 'detalhe'])
            writer.writerows(self.rows)
