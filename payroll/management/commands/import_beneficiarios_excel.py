"""
Management command: import_beneficiarios_excel

Importa a lista de beneficiários do Excel para o openIMIS, executando o fluxo
completo de criação de dados:

  Fase 1 (sempre):
    - Criar ou actualizar Individual (nome, data_n, sexo, BI, NIB, PAN, etc.)
    - Criar ou actualizar Group (agregado_id/NIA como code)
    - Criar ou actualizar GroupIndividual (HEAD + PRIMARY)

  Fase 2 (--benefit-plan-id):
    - BenefitPlan GROUP      → GroupBeneficiary (Group→BenefitPlan, status ACTIVE)
    - BenefitPlan INDIVIDUAL → Beneficiary (Individual→BenefitPlan, status ACTIVE)
    - Só para registos com NIB válido

  Fase 3 (--payroll-id):
    - Criar BenefitConsumption (amount=valor, status=ACCEPTED) — só com NIB válido
    - Criar PayrollBenefitConsumption (ligar ao Payroll)

Agregado já importado do CSU 2026 (import_csu_2026):
    O Group com code = NIA tem json_ext.csu2026_agregado. O titular é procurado entre os membros
    (BI, depois nome) e não se criam pessoas: grava-se o NIB/PAN/subconta, o titular passa a
    destinatário principal (PRIMARY) e seguem as Fases 2 e 3. INDEX diferente do CSU ou titular
    fora da composição do agregado → CONFLITO.

--correcoes: CSV de correcções DPSSF; as linhas INDEX_LISTA corrigem o INDEX CSU2026 da lista.
BI repetido no ficheiro: as linhas são importadas como pessoas distintas e sinalizadas.

Agregado que não veio do CSU — correspondência com registos existentes (hierarquia):
    1. INDEX CSU2026  (json_ext.index_csu2026)
    2. NIA            (json_ext.agregado_id)
    3. BI             (json_ext.bilhete_didentidade)
  Se os identificadores apontarem para Individuals diferentes, se o registo
  encontrado tiver NIA/INDEX/BI diferente do Excel, ou se o agregado já tiver
  outro chefe, a linha NÃO é alterada e fica no relatório de conflitos.
  O NIA de um registo existente nunca é alterado.

Identificadores (NIA, BI, PAN, subconta, NIB, INDEX CSU2026) são tratados como
texto. Se a célula estiver formatada como número no Excel é emitido um aviso; se
o valor numérico já perdeu precisão (>= 15 dígitos) a linha é rejeitada.

O --dry-run executa todas as fases em simulação (incluindo 2 e 3) e lista
conflitos, registos sem NIB e avisos de formatação, sem gravar nada.

Uso:
    # Simulação completa
    python manage.py import_beneficiarios_excel /path/lista.xlsx --dry-run \\
        --benefit-plan-id <uuid> --payroll-id <uuid>

    # Só Fase 1 (indivíduos e agregados)
    python manage.py import_beneficiarios_excel /path/lista.xlsx --username admin

    # Fluxo completo
    python manage.py import_beneficiarios_excel /path/lista.xlsx \\
        --benefit-plan-id <uuid> \\
        --payroll-id <uuid> \\
        --username admin

Colunas esperadas no Excel:
    nome_chefe, distrito, categoria_eligibilidade, agregado_id,
    sexo_chefe, data_n, bilhete_didentidade, pan, subconta, NIBs, valor, Situação, INDEX CSU2026
"""
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from payroll import csu2026
from payroll.csu2026 import normalize_key, normalize_name

logger = logging.getLogger(__name__)

# Colunas tratadas como identificadores (texto) → nome usado nos relatórios
ID_COLUMNS = {
    'agregado_id': 'NIA',
    'bilhete_didentidade': 'BI',
    'pan': 'PAN',
    'subconta': 'subconta',
    'NIBs': 'NIB',
    'INDEX CSU2026': 'INDEX CSU2026',
}

# Um double só representa inteiros exactos até 15 dígitos
MAX_EXACT_FLOAT = 10 ** 15


class _DryRunRollback(Exception):
    pass


class _RowConflict(Exception):
    pass


class Command(BaseCommand):
    help = 'Importa beneficiários do Excel: Individual → Group → GroupIndividual → [Beneficiary] → [BenefitConsumption]'

    def add_arguments(self, parser):
        parser.add_argument('excel_path', type=str,
                            help='Caminho para o ficheiro Excel (.xlsx)')
        parser.add_argument('--dry-run', action='store_true', default=False,
                            help='Simula sem guardar nada na base de dados')
        parser.add_argument('--username', type=str, default='admin',
                            help='Username openIMIS para auditoria (default: admin)')
        parser.add_argument('--sheet', type=str, default=None,
                            help='Nome da sheet (default: primeira sheet)')
        parser.add_argument('--benefit-plan-id', type=str, default=None,
                            help='UUID do BenefitPlan — activa Fase 2 (Beneficiary/GroupBeneficiary)')
        parser.add_argument('--payroll-id', type=str, default=None,
                            help='UUID do Payroll — activa Fase 3 (BenefitConsumption)')
        parser.add_argument('--benefit-type', type=str, default='CASH',
                            help='Tipo de benefício para BenefitConsumption (default: CASH)')
        parser.add_argument('--correcoes', type=str, default=None,
                            help='CSV de correcções DPSSF; usa as linhas INDEX_LISTA (INDEX errado → correcto)')

    def handle(self, *args, **options):
        try:
            import openpyxl
        except ImportError:
            raise CommandError("openpyxl não instalado. Execute: pip install openpyxl")

        from payroll.models import Payroll

        excel_path = Path(options['excel_path'])
        if not excel_path.exists():
            raise CommandError(f"Ficheiro não encontrado: {excel_path}")

        dry_run = options['dry_run']
        username = options['username']
        benefit_plan_id = options['benefit_plan_id']
        payroll_id = options['payroll_id']
        benefit_type = options['benefit_type']

        # HistoryModel.save() exige um User existente — validar antes de processar as linhas
        from core.models import User
        if not User.objects.filter(username=username).exists():
            raise CommandError(f"Utilizador openIMIS não encontrado: {username}")

        self.stdout.write(f"[import] Ficheiro : {excel_path}")
        self.stdout.write(f"[import] dry-run  : {dry_run}")
        self.stdout.write(f"[import] username  : {username}")
        self.stdout.write(f"[import] Fase 2 (Beneficiary)       : {'SIM — ' + benefit_plan_id if benefit_plan_id else 'NÃO'}")
        self.stdout.write(f"[import] Fase 3 (BenefitConsumption): {'SIM — ' + payroll_id if payroll_id else 'NÃO'}")
        self.stdout.write("")

        # Carregar Payroll e BenefitPlan se necessário
        payroll = None
        benefit_plan = None

        if payroll_id:
            try:
                payroll = Payroll.objects.get(id=payroll_id, is_deleted=False)
                self.stdout.write(f"[import] Payroll encontrado: {payroll.name}")
            except Payroll.DoesNotExist:
                raise CommandError(f"Payroll não encontrado: {payroll_id}")

        if benefit_plan_id:
            try:
                from social_protection.models import BenefitPlan
                benefit_plan = BenefitPlan.objects.get(id=benefit_plan_id, is_deleted=False)
                self.stdout.write(f"[import] BenefitPlan encontrado: {benefit_plan.name} ({benefit_plan.type})")
            except ImportError:
                raise CommandError("Módulo social_protection não disponível")
            except Exception:
                raise CommandError(f"BenefitPlan não encontrado: {benefit_plan_id}")

        index_fixes = {}
        # .get: a tarefa Celery chama handle() directamente, sem esta opção
        correcoes_path = options.get('correcoes')
        if correcoes_path:
            try:
                index_fixes = csu2026.list_index_corrections(csu2026.load_corrections(correcoes_path))
            except csu2026.CsuFileError as exc:
                raise CommandError(str(exc))

        # Pré-carregar localidades
        location_cache = csu2026.build_district_cache()

        # Ler Excel
        wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
        sheet_name = options['sheet'] or wb.sheetnames[0]
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))

        if not rows:
            raise CommandError("Excel está vazio")

        headers = [str(h).strip() if h is not None else '' for h in rows[0]]
        self.stdout.write(f"[import] Colunas detectadas: {headers}")
        self.stdout.write(f"[import] Total de linhas de dados: {len(rows) - 1}")
        self.stdout.write("")

        def col(row, name, default=None):
            try:
                idx = headers.index(name)
                v = row[idx]
                return v if v is not None else default
            except ValueError:
                return default

        # Contadores
        stats = {
            'total': 0,
            'ind_created': 0, 'ind_updated': 0,
            'group_created': 0,
            'gi_created': 0,
            'benef_created': 0, 'benef_reactivated': 0,
            'bc_created': 0,
            'titular_csu': 0, 'primary_changed': 0,
            'conflicts': [],
            'correcoes': [],
            'bi_repetido': [],
            'sem_nib': [],
            'warnings': [],
            'errors': [],
        }

        # Individuals já usados por uma linha deste ficheiro: o mesmo BI em linhas diferentes
        # (outro agregado) dá origem a pessoas distintas em vez de sobrescrever a anterior
        claimed_ids = set()

        # Identificadores já vistos neste ficheiro (detectar duplicados no Excel)
        seen = {'INDEX CSU2026': {}, 'NIA': {}, 'BI': {}}

        for line_num, row in enumerate(rows[1:], start=2):
            if all(v is None for v in row):
                continue

            stats['total'] += 1

            nome_chefe = str(col(row, 'nome_chefe', '')).strip()
            distrito = str(col(row, 'distrito', '')).strip().upper()
            sexo = str(col(row, 'sexo_chefe', '')).strip().upper()
            data_n_raw = col(row, 'data_n')
            valor_raw = col(row, 'valor')
            categoria = str(col(row, 'categoria_eligibilidade', '')).strip()

            # Identificadores como texto
            ids = {}
            lossy = []
            for column, label in ID_COLUMNS.items():
                text, numeric, lost = self._as_text(col(row, column))
                ids[column] = text
                if lost:
                    lossy.append(f"{label} (valor lido: {text})")
                elif numeric:
                    stats['warnings'].append(
                        f"Linha {line_num}: {label} formatado como número no Excel (lido: {text}) "
                        f"— confirmar que não perdeu zeros à esquerda"
                    )

            agregado_id = ids['agregado_id']
            bi_str = ids['bilhete_didentidade'] or None
            pan = ids['pan']
            subconta = ids['subconta']
            nib = ids['NIBs']
            index_csu = ids['INDEX CSU2026']

            if not nome_chefe or not agregado_id:
                stats['errors'].append(f"Linha {line_num}: nome ou agregado_id em falta — ignorado")
                continue

            if lossy:
                stats['errors'].append(
                    f"Linha {line_num}: {nome_chefe} (NIA={agregado_id}) — célula numérica com perda de "
                    f"precisão em {', '.join(lossy)}. Formatar a coluna como Texto no Excel — ignorado"
                )
                continue

            if index_csu in index_fixes:
                fix = index_fixes[index_csu]
                stats['correcoes'].append(f"Linha {line_num}: {nome_chefe} (NIA={agregado_id}) — {fix.describe()}")
                index_csu = fix.new_value

            duplicated = self._check_duplicates_in_file(
                seen, line_num, {'INDEX CSU2026': index_csu, 'NIA': agregado_id}
            )
            if duplicated:
                stats['conflicts'].append(f"Linha {line_num}: {nome_chefe} (NIA={agregado_id}) — {duplicated}")
                continue
            # BI repetido: importar e sinalizar, sem fusão (decisão DPSSF de 23/11)
            repeated_bi = self._check_duplicates_in_file(seen, line_num, {'BI': bi_str})
            if repeated_bi:
                stats['bi_repetido'].append(f"Linha {line_num}: {nome_chefe} (NIA={agregado_id}) — {repeated_bi}")

            # Processar campos
            first_name, last_name = self._split_name(nome_chefe)
            dob = self._parse_date(data_n_raw)
            amount = self._parse_decimal(valor_raw)
            location = location_cache.get(normalize_key(distrito))

            try:
                # ──────────────────────────────
                # Correspondência + conflitos (antes de qualquer escrita)
                # Agregado já importado do CSU → titular entre os membros; senão, hierarquia antiga
                # ──────────────────────────────
                resolved = self._resolve_csu_titular(
                    index_csu=index_csu, agregado_id=agregado_id, bi_str=bi_str, nome=nome_chefe,
                )
                csu_titular = resolved is not None
                if csu_titular:
                    individual, group = resolved
                else:
                    individual, group = self._resolve_existing(
                        index_csu=index_csu, agregado_id=agregado_id, bi_str=bi_str,
                        claimed_ids=claimed_ids,
                    )
            except _RowConflict as conflict:
                stats['conflicts'].append(f"Linha {line_num}: {nome_chefe} (NIA={agregado_id}) — {conflict}")
                continue

            try:
                with transaction.atomic():
                    if csu_titular:
                        # ──────────────────────────────
                        # FASE 1 (agregado do CSU) — titular entre os membros, sem criar pessoas
                        # ──────────────────────────────
                        self._update_csu_titular(
                            individual=individual, bi_str=bi_str, dob=dob, nib=nib, pan=pan,
                            subconta=subconta, categoria=categoria, index_csu=index_csu,
                            username=username, dry_run=dry_run,
                        )
                        stats['ind_updated'] += 1
                        stats['titular_csu'] += 1
                        if self._ensure_primary_recipient(group=group, individual=individual,
                                                          username=username, dry_run=dry_run):
                            stats['primary_changed'] += 1
                        effective_nib = self._effective_nib(individual)
                        self.stdout.write(
                            f"  {'[DRY] ' if dry_run else ''}Linha {line_num}: {nome_chefe} "
                            f"(agregado={agregado_id}) → titular do agregado CSU ACTUALIZADO"
                            + (f" | NIB={effective_nib[:6]}***" if effective_nib else " | SEM NIB")
                        )
                    else:
                        # ──────────────────────────────
                        # FASE 1A — Individual
                        # ──────────────────────────────
                        individual, ind_created = self._create_or_update_individual(
                            individual=individual,
                            agregado_id=agregado_id,
                            bi_str=bi_str,
                            first_name=first_name,
                            last_name=last_name,
                            dob=dob,
                            nib=nib,
                            sexo=sexo,
                            pan=pan,
                            subconta=subconta,
                            categoria=categoria,
                            index_csu=index_csu,
                            location=location,
                            username=username,
                            dry_run=dry_run,
                        )
                        claimed_ids.add(individual.id)

                        if ind_created:
                            stats['ind_created'] += 1
                            action = 'CRIADO'
                        else:
                            stats['ind_updated'] += 1
                            action = 'ACTUALIZADO'

                        effective_nib = self._effective_nib(individual)
                        self.stdout.write(
                            f"  {'[DRY] ' if dry_run else ''}Linha {line_num}: {nome_chefe} "
                            f"(agregado={agregado_id}) → Individual {action}"
                            + (f" | NIB={effective_nib[:6]}***" if effective_nib else " | SEM NIB")
                        )

                        # ──────────────────────────────
                        # FASE 1B — Group
                        # ──────────────────────────────
                        group, grp_created = self._create_or_update_group(
                            group=group,
                            agregado_id=agregado_id,
                            location=location,
                            username=username,
                            dry_run=dry_run,
                        )
                        if grp_created:
                            stats['group_created'] += 1

                        # ──────────────────────────────
                        # FASE 1C — GroupIndividual
                        # ──────────────────────────────
                        gi_created = self._ensure_group_individual(
                            group=group,
                            individual=individual,
                            username=username,
                            dry_run=dry_run,
                        )
                        if gi_created:
                            stats['gi_created'] += 1

                    # Sem NIB o agregado fica registado, mas as Fases 2 e 3 não correm
                    if not effective_nib:
                        stats['sem_nib'].append(
                            f"Linha {line_num}: {nome_chefe} (NIA={agregado_id}) — agregado registado, "
                            f"não habilitado para pagamento"
                        )
                    else:
                        # ──────────────────────────────
                        # FASE 2 — Beneficiary / GroupBeneficiary
                        # ──────────────────────────────
                        if benefit_plan:
                            benef_result = self._ensure_beneficiary(
                                individual=individual,
                                group=group,
                                benefit_plan=benefit_plan,
                                username=username,
                                dry_run=dry_run,
                            )
                            if benef_result == 'created':
                                stats['benef_created'] += 1
                            elif benef_result == 'reactivated':
                                stats['benef_reactivated'] += 1

                        # ──────────────────────────────
                        # FASE 3 — BenefitConsumption
                        # ──────────────────────────────
                        if payroll and amount is not None:
                            bc_created = self._ensure_benefit_consumption(
                                individual=individual,
                                payroll=payroll,
                                agregado_id=agregado_id,
                                amount=amount,
                                benefit_type=benefit_type,
                                username=username,
                                dry_run=dry_run,
                            )
                            if bc_created:
                                stats['bc_created'] += 1

                    if dry_run:
                        raise _DryRunRollback()

            except _DryRunRollback:
                pass
            except Exception as exc:
                msg = f"Linha {line_num}: {nome_chefe} — ERRO: {exc}"
                stats['errors'].append(msg)
                self.stdout.write(self.style.ERROR(f"  {msg}"))
                logger.exception("[import_beneficiarios_excel] %s", msg)

        self._print_summary(stats, dry_run, benefit_plan_id, payroll_id)

    # ────────────────────────────────────────────────────────
    # Relatório
    # ────────────────────────────────────────────────────────

    def _print_summary(self, stats, dry_run, benefit_plan_id, payroll_id):
        self.stdout.write("")
        self.stdout.write("=" * 65)
        self.stdout.write(f"Total processado          : {stats['total']}")
        self.stdout.write(f"Indivíduos criados        : {stats['ind_created']}")
        self.stdout.write(f"Indivíduos actualizados   : {stats['ind_updated']}")
        self.stdout.write(f"Grupos criados            : {stats['group_created']}")
        self.stdout.write(f"Ligações grupo criadas    : {stats['gi_created']}")
        self.stdout.write(f"Titulares de agregado CSU : {stats['titular_csu']}")
        self.stdout.write(f"Destinatário principal mudado: {stats['primary_changed']}")
        self.stdout.write(f"Correcções DPSSF aplicadas: {len(stats['correcoes'])}")
        self.stdout.write(f"BI repetido (sinalizado)  : {len(stats['bi_repetido'])}")
        if benefit_plan_id:
            self.stdout.write(f"Beneficiários criados     : {stats['benef_created']}")
            self.stdout.write(f"Beneficiários reactivados : {stats['benef_reactivated']}")
        if payroll_id:
            self.stdout.write(f"BenefitConsumption criados: {stats['bc_created']}")
        self.stdout.write(f"Conflitos (não alterados) : {len(stats['conflicts'])}")
        self.stdout.write(f"Sem NIB (não habilitados) : {len(stats['sem_nib'])}")
        self.stdout.write(f"Avisos de formatação      : {len(stats['warnings'])}")
        self.stdout.write(f"Erros                     : {len(stats['errors'])}")
        if dry_run:
            self.stdout.write(self.style.WARNING("MODO DRY-RUN — nenhuma alteração foi guardada"))

        sections = [
            ('CONFLITOS — linhas não alteradas, requerem análise', stats['conflicts'], self.style.ERROR),
            ('CORRECÇÕES DPSSF APLICADAS', stats['correcoes'], self.style.WARNING),
            ('BI REPETIDO NO FICHEIRO — importado e sinalizado, sem fusão', stats['bi_repetido'], self.style.WARNING),
            ('SEM NIB VÁLIDO — não habilitados para pagamento', stats['sem_nib'], self.style.WARNING),
            ('AVISOS DE FORMATAÇÃO', stats['warnings'], self.style.WARNING),
            ('ERROS', stats['errors'], self.style.ERROR),
        ]
        for title, items, style in sections:
            if items:
                self.stdout.write(style(f"\n{title} ({len(items)}):"))
                for item in items:
                    self.stdout.write(style(f"  {item}"))

    # ────────────────────────────────────────────────────────
    # Helpers
    # ────────────────────────────────────────────────────────

    def _split_name(self, full_name):
        parts = full_name.strip().split(' ', 1)
        first_name = parts[0] if parts else ''
        last_name = parts[1] if len(parts) > 1 else ''
        return first_name, last_name

    def _parse_date(self, raw):
        if isinstance(raw, datetime):
            return raw.date()
        if isinstance(raw, date):
            return raw
        if isinstance(raw, str):
            for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
                try:
                    return datetime.strptime(raw.strip(), fmt).date()
                except ValueError:
                    pass
        return None

    def _as_text(self, raw):
        """
        Converte o valor de uma célula-identificador para texto, sem alterar o conteúdo.
        Devolve (texto, era_numérico, perdeu_precisão).
        """
        if raw is None:
            return '', False, False
        if isinstance(raw, bool):
            return str(raw), False, False
        if isinstance(raw, int):
            return str(raw), True, abs(raw) >= MAX_EXACT_FLOAT
        if isinstance(raw, float):
            if raw.is_integer():
                return str(int(raw)), True, abs(raw) >= MAX_EXACT_FLOAT
            return str(raw), True, True
        return str(raw).strip(), False, False

    def _parse_decimal(self, raw):
        if raw is None:
            return None
        try:
            return Decimal(str(raw))
        except InvalidOperation:
            return None

    def _check_duplicates_in_file(self, seen, line_num, identifiers):
        for label, value in identifiers.items():
            if not value:
                continue
            first_line = seen[label].get(value)
            if first_line:
                return f"{label} {value} repetido no ficheiro (já na linha {first_line})"
        for label, value in identifiers.items():
            if value:
                seen[label][value] = line_num
        return None

    def _effective_nib(self, individual):
        # Mesma ordem usada pelo StrategyBistpPayment no envio
        json_ext = individual.json_ext or {}
        nib = json_ext.get('NIB') or json_ext.get('nib') or getattr(individual, 'nib', None)
        return str(nib).strip() if nib else ''

    def _resolve_csu_titular(self, *, index_csu, agregado_id, bi_str, nome):
        """
        Agregado já importado do CSU (json_ext.csu2026_agregado): o titular da lista é procurado
        entre os membros, por BI e depois pelo nome. Devolve (Individual, Group), ou None quando
        o agregado não veio do CSU (segue a hierarquia INDEX → NIA → BI).
        """
        from individual.models import Group, GroupIndividual

        groups = list(Group.objects.filter(code=agregado_id, is_deleted=False)[:2])
        if len(groups) > 1:
            raise _RowConflict(f"existe mais do que um agregado activo com NIA {agregado_id}")
        if not groups:
            return None
        group = groups[0]
        csu_data = (group.json_ext or {}).get('csu2026_agregado')
        if not csu_data:
            return None

        csu_index = csu2026.as_text(csu_data.get('index'))
        if index_csu and csu_index and csu_index != index_csu:
            raise _RowConflict(f"INDEX da lista ({index_csu}) diferente do INDEX do agregado no CSU ({csu_index})")

        members = [
            gi.individual for gi in
            GroupIndividual.objects.filter(group=group, is_deleted=False).select_related('individual')
            if not gi.individual.is_deleted
        ]

        def stored_bi(individual):
            return csu2026.as_text((individual.json_ext or {}).get('bilhete_didentidade'))

        if bi_str:
            by_bi = [m for m in members if stored_bi(m) == bi_str]
            if len(by_bi) == 1:
                return by_bi[0], group
            if len(by_bi) > 1:
                raise _RowConflict(f"BI {bi_str} corresponde a mais do que um membro do agregado")

        wanted = normalize_name(nome)
        by_name = [
            m for m in members
            if csu2026.person_name_key(m.first_name, m.last_name) == wanted
            and not (bi_str and stored_bi(m) and stored_bi(m) != bi_str)
        ]
        if len(by_name) == 1:
            return by_name[0], group
        if len(by_name) > 1:
            raise _RowConflict("o nome corresponde a mais do que um membro do agregado")
        raise _RowConflict(
            f"o titular não consta da composição do agregado no CSU (INDEX {csu_index or index_csu})"
        )

    def _update_csu_titular(self, *, individual, bi_str, dob, nib, pan, subconta, categoria,
                            index_csu, username, dry_run):
        """
        Titular que já veio do CSU: grava os dados de pagamento da lista. Nome, sexo e parentesco
        ficam os do CSU; a data de nascimento da lista só substitui uma data estimada.
        """
        json_ext = dict(individual.json_ext or {})
        if nib:
            individual.nib = nib
            for key in ('NIB', 'nib'):
                if key in json_ext:
                    json_ext[key] = nib
        if pan:
            json_ext['pan'] = pan
        if subconta:
            json_ext['subconta'] = subconta
        if categoria:
            json_ext['categoria_eligibilidade'] = categoria
        if index_csu:
            json_ext['index_csu2026'] = index_csu
        if bi_str and not json_ext.get('bilhete_didentidade'):
            json_ext['bilhete_didentidade'] = bi_str

        csu_member = dict(json_ext.get('csu2026') or {})
        if dob and csu_member.get('dob_estimada'):
            individual.dob = dob
            csu_member['dob_estimada'] = False
            csu_member['dob_fonte'] = 'lista de pagamento'
            json_ext['csu2026'] = csu_member
        individual.json_ext = json_ext

        if not dry_run and individual.is_dirty(check_relationship=True):
            individual.save(username=username)

    def _ensure_primary_recipient(self, *, group, individual, username, dry_run):
        """O titular da lista passa a destinatário principal; o serviço do grupo despromove o anterior."""
        from individual.models import GroupIndividual

        gi = GroupIndividual.objects.filter(group=group, individual=individual, is_deleted=False).first()
        if gi is None or gi.recipient_type == GroupIndividual.RecipientType.PRIMARY:
            return False
        if not dry_run:
            gi.recipient_type = GroupIndividual.RecipientType.PRIMARY
            gi.save(username=username)
        return True

    def _resolve_existing(self, *, index_csu, agregado_id, bi_str, claimed_ids=()):
        """
        Procura o Individual e o Group existentes pela hierarquia INDEX CSU2026 → NIA → BI.
        Levanta _RowConflict sempre que os dados não permitem uma correspondência segura.
        """
        from individual.models import Individual, Group, GroupIndividual

        active = Individual.objects.filter(is_deleted=False)
        lookups = []
        if index_csu:
            lookups.append(('INDEX CSU2026', active.filter(json_ext__index_csu2026=index_csu)))
        if agregado_id:
            lookups.append(('NIA', active.filter(json_ext__agregado_id=agregado_id)))
        if bi_str:
            bi_q = Q(json_ext__bilhete_didentidade=bi_str)
            if bi_str.isdigit():
                bi_q |= Q(json_ext__bilhete_didentidade=int(bi_str))
            # Individuals já atribuídos a outra linha do ficheiro ficam de fora (BI repetido → pessoas distintas)
            lookups.append(('BI', active.filter(bi_q).exclude(id__in=claimed_ids)))

        matches = {}
        for label, queryset in lookups:
            found = list(queryset[:2])
            if len(found) > 1:
                raise _RowConflict(f"{label} corresponde a mais do que um Individual no sistema")
            if found:
                matches[label] = found[0]

        distinct = {ind.id: ind for ind in matches.values()}
        if len(distinct) > 1:
            detail = ', '.join(f"{label}→{ind.first_name} {ind.last_name} ({ind.id})"
                               for label, ind in matches.items())
            raise _RowConflict(f"identificadores apontam para Individuals diferentes: {detail}")

        individual = next(iter(distinct.values()), None)

        if individual is not None:
            stored = individual.json_ext or {}
            for label, row_value, key in (
                    ('NIA', agregado_id, 'agregado_id'),
                    ('INDEX CSU2026', index_csu, 'index_csu2026'),
                    ('BI', bi_str, 'bilhete_didentidade'),
            ):
                stored_value = stored.get(key)
                if row_value and stored_value not in (None, '') and str(stored_value).strip() != row_value:
                    raise _RowConflict(
                        f"Individual existente ({individual.id}) tem {label}={stored_value}, Excel tem {row_value}"
                    )

            other_group = GroupIndividual.objects.filter(
                individual=individual, is_deleted=False, group__is_deleted=False,
            ).exclude(group__code=agregado_id).select_related('group').first()
            if other_group:
                raise _RowConflict(
                    f"Individual existente ({individual.id}) já pertence ao agregado {other_group.group.code}"
                )

        groups = list(Group.objects.filter(code=agregado_id, is_deleted=False)[:2])
        if len(groups) > 1:
            raise _RowConflict(f"existe mais do que um agregado activo com NIA {agregado_id}")
        group = groups[0] if groups else None

        if group is not None:
            other_heads = GroupIndividual.objects.filter(
                group=group, role=GroupIndividual.Role.HEAD, is_deleted=False,
            )
            if individual is not None:
                other_heads = other_heads.exclude(individual=individual)
            other_head = other_heads.select_related('individual').first()
            if other_head:
                head = other_head.individual
                raise _RowConflict(
                    f"agregado {agregado_id} já tem outro chefe: {head.first_name} {head.last_name} ({head.id})"
                )

        return individual, group

    def _create_or_update_individual(self, *, individual, agregado_id, bi_str, first_name, last_name,
                                     dob, nib, sexo, pan, subconta, categoria, index_csu,
                                     location, username, dry_run):
        from individual.models import Individual

        created = individual is None
        if created:
            individual = Individual(
                first_name=first_name,
                last_name=last_name,
            )

        # Actualizar campos
        individual.first_name = first_name
        individual.last_name = last_name
        if dob:
            individual.dob = dob
        elif created and not individual.dob:
            # dob é obrigatório no modelo — usar data placeholder quando ausente no Excel
            individual.dob = date(1900, 1, 1)
        if location:
            individual.location = location

        json_ext = individual.json_ext or {}
        if nib:
            individual.nib = nib
            # O envio BISTP lê primeiro json_ext — manter as cópias existentes alinhadas
            for key in ('NIB', 'nib'):
                if key in json_ext:
                    json_ext[key] = nib
        if bi_str:
            json_ext['bilhete_didentidade'] = bi_str
        if pan:
            json_ext['pan'] = pan
        if subconta:
            json_ext['subconta'] = subconta
        if sexo:
            json_ext['sexo'] = sexo
        if categoria:
            json_ext['categoria_eligibilidade'] = categoria
        if index_csu:
            json_ext['index_csu2026'] = index_csu
        # NIA nunca é alterado: num registo existente é igual (caso contrário seria conflito)
        json_ext.setdefault('agregado_id', agregado_id)
        individual.json_ext = json_ext

        if not dry_run:
            # HistoryModel.save() recusa guardar se nada mudou — verificar is_dirty()
            if created or individual.is_dirty():
                individual.save(username=username)

        return individual, created

    def _create_or_update_group(self, *, group, agregado_id, location, username, dry_run):
        from individual.models import Group

        created = False

        if group is None:
            group = Group(code=agregado_id)
            if location:
                group.location = location
            created = True
            if not dry_run:
                group.save(username=username)
        elif location and group.location_id != location.id:
            group.location = location
            if not dry_run:
                group.save(username=username)

        return group, created

    def _ensure_group_individual(self, *, group, individual, username, dry_run):
        from individual.models import GroupIndividual

        if not group._state.adding and not individual._state.adding and GroupIndividual.objects.filter(
            group=group, individual=individual, is_deleted=False
        ).exists():
            return False

        if not dry_run:
            gi = GroupIndividual(
                group=group,
                individual=individual,
                role=GroupIndividual.Role.HEAD,
                recipient_type=GroupIndividual.RecipientType.PRIMARY,
            )
            gi.save(username=username)

        return True

    def _ensure_beneficiary(self, *, individual, group, benefit_plan, username, dry_run):
        try:
            from social_protection.models import Beneficiary, GroupBeneficiary, BeneficiaryStatus, BenefitPlan
        except ImportError:
            logger.warning("[import] social_protection não disponível — Fase 2 ignorada")
            return None

        if benefit_plan.type == BenefitPlan.BenefitPlanType.GROUP_TYPE:
            model, target_field, target = GroupBeneficiary, 'group', group
        else:
            model, target_field, target = Beneficiary, 'individual', individual

        existing = None
        if not target._state.adding:
            existing = model.objects.filter(
                **{target_field: target},
                benefit_plan=benefit_plan,
                is_deleted=False,
            ).first()

        if existing:
            if existing.status != BeneficiaryStatus.ACTIVE:
                if not dry_run:
                    existing.status = BeneficiaryStatus.ACTIVE
                    existing.save(username=username)
                return 'reactivated'
            return None

        if not dry_run:
            benef = model(
                **{target_field: target},
                benefit_plan=benefit_plan,
                status=BeneficiaryStatus.ACTIVE,
            )
            benef.save(username=username)
        return 'created'

    def _ensure_benefit_consumption(self, *, individual, payroll, agregado_id,
                                    amount, benefit_type, username, dry_run):
        from payroll.models import BenefitConsumption, BenefitConsumptionStatus, PayrollBenefitConsumption

        # Verificar se já existe para este payroll e individual
        if not individual._state.adding and BenefitConsumption.objects.filter(
            individual=individual,
            payrollbenefitconsumption__payroll=payroll,
            is_deleted=False,
        ).exists():
            return False

        if dry_run:
            return True

        today = date.today()
        code = f"{agregado_id}-{today.strftime('%Y%m')}"

        bc = BenefitConsumption(
            individual=individual,
            code=code,
            amount=amount,
            type=benefit_type,
            status=BenefitConsumptionStatus.ACCEPTED,
            date_due=today,
        )
        bc.save(username=username)

        pbc = PayrollBenefitConsumption(
            payroll=payroll,
            benefit=bc,
        )
        pbc.save(username=username)

        return True
