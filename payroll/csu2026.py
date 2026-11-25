"""
Leitura do ficheiro do CSU 2026 (exportação XLSX do KoboToolbox) e regras de mapeamento.

Este módulo só lê o ficheiro e devolve agregados e membros já normalizados, para ser usado
pelo comando import_csu_2026 e testado isoladamente. A única função com acesso à base de dados
é build_district_cache().

Folhas usadas (identificadas pelo nome):
    Atualização_Registo CSU 2026  → um agregado por entrevista (_index)
    repeat_id_memb                → membros (_parent_index → _index do agregado)
    group_outros_dados            → dados complementares do membro
                                    (_parent_index, indice_membro_atual) = (_parent_index, pos_memb)
    group_th5sq22                 → ex-membros emigrados (_parent_index)

As colunas das perguntas são lidas pelo código da pergunta (B2, C1, F10-b, ...) e não pelo
texto completo, que pode mudar entre versões do formulário.
"""
import csv
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

SHEET_HOUSEHOLDS = 'Atualização_Registo CSU 2026'
SHEET_MEMBERS = 'repeat_id_memb'
SHEET_MEMBER_DATA = 'group_outros_dados'
SHEET_EMIGRANTS = 'group_th5sq22'

COL_NIA = 'NIA_AGREGADO_VALIDADO'
COL_NIA_ORIGINAL = 'NIA_ORIGINAL_CSU'

# Código da pergunta no início do cabeçalho: "F10-b): Nome da escola", "C1: Tipo", "E7-a): ..."
QUESTION_CODE = re.compile(r'^([A-Z]\d+(?:-[a-z])?)\)?\s*:?\s*\)?')

# Respostas que não são guardadas (assinaturas e confirmações do inquiridor)
SKIPPED_CODES = {'R1', 'R2'}

# Valores de E6-a que significam "sem documento"
EMPTY_DOCUMENT_NUMBERS = {'0', '00', '000', '0000'}

PRESENT = '1'

# Parentesco (F1) → GroupIndividual.Role. Os pares dependem do sexo (M, F).
ROLE_BY_RELATION = (
    ('CHEFE', ('HEAD', 'HEAD')),
    ('CONJUGE', ('SPOUSE', 'SPOUSE')),
    ('ENTEADO', ('OTHER RELATIVE', 'OTHER RELATIVE')),
    ('FILHO', ('SON', 'DAUGHTER')),
    ('NETO', ('GRANDSON', 'GRANDDAUGHTER')),
    ('PAI/MAE', ('FATHER', 'MOTHER')),
    ('IRMAO', ('BROTHER', 'SISTER')),
    ('AVO', ('GRANDFATHER', 'GRANDMOTHER')),
    ('GENRO', ('OTHER RELATIVE', 'OTHER RELATIVE')),
    ('OUTRO PARENTE', ('OTHER RELATIVE', 'OTHER RELATIVE')),
    ('DOMESTICA', ('OTHER RELATIVE', 'OTHER RELATIVE')),
    ('OUTRO SEM PARENTESCO', ('NOT RELATED', 'NOT RELATED')),
)

# Distritos: chave normalizada do ficheiro → chaves normalizadas aceites para o nome da Location
DISTRICT_ALIASES = {
    'AGUAGRANDE': ['AGUAGRANDE'],
    'MEZOCHI': ['MEZOCHI'],
    'CANTAGALO': ['CANTAGALO'],
    'LEMBA': ['LEMBA'],
    'LOBATA': ['LOBATA'],
    'CAUE': ['CAUE'],
    'PAGUE': ['PAGUE', 'PRINCIPE', 'RAP', 'REGIAOAUTONOMADOPRINCIPE'],
    'RAP': ['PAGUE', 'PRINCIPE', 'RAP', 'REGIAOAUTONOMADOPRINCIPE'],
}

CORRECTION_NIA = 'NIA'
CORRECTION_LIST_INDEX = 'INDEX_LISTA'
CORRECTION_NO_MEMBERS_EXCEPTION = 'EXCECAO_SEM_MEMBROS'
CORRECTION_TYPES = {CORRECTION_NIA, CORRECTION_LIST_INDEX, CORRECTION_NO_MEMBERS_EXCEPTION}


class CsuFileError(Exception):
    pass


# ────────────────────────────────────────────────────────────
# Normalização
# ────────────────────────────────────────────────────────────

def strip_accents(value):
    return unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode()


def normalize_name(value):
    """Nome para comparação: sem acentos, maiúsculas, espaços simples."""
    if value is None:
        return ''
    return re.sub(r'\s+', ' ', strip_accents(str(value)).upper()).strip()


def normalize_key(value):
    """Chave só com letras e dígitos: 'MÉ-ZÓCHI' → 'MEZOCHI'."""
    return re.sub(r'[^A-Z0-9]', '', normalize_name(value))


def as_text(value):
    """Identificador como texto (inteiros do Excel sem '.0')."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def split_name(full_name):
    parts = (full_name or '').strip().split(' ', 1)
    return parts[0], (parts[1].strip() if len(parts) > 1 else '')


def to_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                pass
    return None


def json_value(value):
    """Valor de célula serializável em JSON."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        return value.strip()
    return value


def role_for(relation, sex):
    key = normalize_name(relation)
    female = sex == 'F'
    for prefix, (male_role, female_role) in ROLE_BY_RELATION:
        if key.startswith(prefix):
            return female_role if female else male_role
    return 'OTHER RELATIVE'


def sex_code(value):
    key = normalize_name(value)
    if key in ('M', '1. MASCULINO', 'MASCULINO'):
        return 'M'
    if key in ('F', '2. FEMININO', 'FEMININO'):
        return 'F'
    return ''


def estimated_dob(age, reference_date):
    """01/01 do ano estimado a partir da idade (decisão DPSSF de 23/11)."""
    try:
        years = int(str(age).strip())
    except (TypeError, ValueError):
        return None
    if years < 0 or years > 130:
        return None
    return date(reference_date.year - years, 1, 1)


# ────────────────────────────────────────────────────────────
# Estruturas
# ────────────────────────────────────────────────────────────

@dataclass
class Member:
    key: str                    # <uuid do agregado>:<pos_memb> — chave de idempotência
    pos: str
    full_name: str
    first_name: str
    last_name: str
    sex: str
    dob: date
    dob_estimated: bool
    relation: str
    role: str
    bi: str
    document_type: str
    document_number: str
    answers: dict = field(default_factory=dict)

    @property
    def is_head(self):
        return self.role == 'HEAD'


@dataclass
class Household:
    index: str
    uuid: str
    nia: str
    nia_original: str
    present: bool
    district: str
    data: dict
    members: list = field(default_factory=list)
    corrections: list = field(default_factory=list)
    respondent_name: str = ''

    @property
    def head(self):
        return next((m for m in self.members if m.is_head), None)


@dataclass
class Correction:
    type: str
    index: str
    previous_value: str
    new_value: str
    reference: str

    def describe(self):
        if self.type == CORRECTION_NIA:
            return f"INDEX {self.index}: NIA {self.previous_value or '?'} → {self.new_value} ({self.reference})"
        if self.type == CORRECTION_LIST_INDEX:
            return f"lista: INDEX {self.index} → {self.new_value} ({self.reference})"
        return f"INDEX {self.index}: excepção à exclusão de agregados sem membros ({self.reference})"


@dataclass
class CsuFile:
    households: dict            # index → Household
    orphan_member_data: int     # linhas de group_outros_dados sem membro


# ────────────────────────────────────────────────────────────
# Correcções DPSSF
# ────────────────────────────────────────────────────────────

def load_corrections(path):
    """
    CSV com as colunas: tipo, index, valor_anterior, valor_novo, referencia.
    Aceita ',' ou ';' como separador.
    """
    with open(path, encoding='utf-8-sig', newline='') as handle:
        sample = handle.read(2048)
        handle.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=',;')
        reader = csv.DictReader(handle, dialect=dialect)
        required = {'tipo', 'index', 'valor_anterior', 'valor_novo', 'referencia'}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise CsuFileError(f"Ficheiro de correcções sem as colunas: {', '.join(sorted(missing))}")
        corrections = []
        for line, row in enumerate(reader, start=2):
            kind = (row['tipo'] or '').strip().upper()
            if kind not in CORRECTION_TYPES:
                raise CsuFileError(f"Correcções, linha {line}: tipo desconhecido '{row['tipo']}'")
            correction = Correction(
                type=kind,
                index=as_text(row['index']),
                previous_value=as_text(row['valor_anterior']),
                new_value=as_text(row['valor_novo']),
                reference=as_text(row['referencia']),
            )
            if not correction.index:
                raise CsuFileError(f"Correcções, linha {line}: index em falta")
            if kind != CORRECTION_NO_MEMBERS_EXCEPTION and not correction.new_value:
                raise CsuFileError(f"Correcções, linha {line}: valor_novo em falta")
            corrections.append(correction)
    return corrections


def list_index_corrections(corrections):
    """INDEX da lista de pagamento → Correction com o INDEX correcto."""
    return {c.index: c for c in corrections if c.type == CORRECTION_LIST_INDEX}


# ────────────────────────────────────────────────────────────
# Leitura do ficheiro
# ────────────────────────────────────────────────────────────

class _Sheet:
    def __init__(self, workbook, name):
        if name not in workbook.sheetnames:
            raise CsuFileError(f"Folha '{name}' não encontrada no ficheiro (folhas: {workbook.sheetnames})")
        rows = workbook[name].iter_rows(values_only=True)
        self.headers = [str(h).strip() if h is not None else '' for h in next(rows, [])]
        self.rows = [row for row in rows if any(v is not None for v in row)]

    def index_of(self, name):
        try:
            return self.headers.index(name)
        except ValueError:
            raise CsuFileError(f"Coluna '{name}' não encontrada")

    def index_by_prefix(self, prefix):
        for i, header in enumerate(self.headers):
            if header.startswith(prefix):
                return i
        raise CsuFileError(f"Coluna '{prefix}...' não encontrada")

    def answer_columns(self):
        """(posição, chave) das colunas com código de pergunta; '_url' para os links do Kobo."""
        columns = []
        for i, header in enumerate(self.headers):
            match = QUESTION_CODE.match(header)
            if not match or match.group(1) in SKIPPED_CODES:
                continue
            # Colunas binárias das escolhas múltiplas ("F8: ...?/Angolana"): guarda-se a coluna de texto
            if '?/' in header:
                continue
            key = match.group(1)
            if header.endswith('_URL'):
                key += '_url'
            columns.append((i, key))
        return columns


def _answers(row, columns):
    answers = {}
    for i, key in columns:
        value = row[i] if i < len(row) else None
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        answers[key] = json_value(value)
    return answers


def read_csu_file(path, corrections=()):
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        main = _Sheet(workbook, SHEET_HOUSEHOLDS)
        members = _Sheet(workbook, SHEET_MEMBERS)
        member_data = _Sheet(workbook, SHEET_MEMBER_DATA)
        emigrants = _Sheet(workbook, SHEET_EMIGRANTS)
    finally:
        workbook.close()

    households = _read_households(main)
    _apply_nia_corrections(households, corrections)
    _check_unique_nias(households)

    data_by_member, orphans = _read_member_data(member_data, members)
    _read_members(members, households, data_by_member)
    _read_emigrants(emigrants, households)

    for correction in corrections:
        if correction.type == CORRECTION_NO_MEMBERS_EXCEPTION:
            household = households.get(correction.index)
            if household is None:
                raise CsuFileError(f"Correcção para INDEX {correction.index} inexistente no CSU")
            household.corrections.append(correction)

    return CsuFile(households=households, orphan_member_data=orphans)


def _read_households(sheet):
    c_index = sheet.index_of('_index')
    c_uuid = sheet.index_of('_uuid')
    c_id = sheet.index_of('_id')
    c_nia = sheet.index_of(COL_NIA)
    c_nia_original = sheet.index_of(COL_NIA_ORIGINAL)
    c_today = sheet.index_of('today')
    c_interviewer = sheet.index_of('nome_inquiridor')
    c_presence = sheet.index_by_prefix('R4:')
    c_respondent = sheet.index_by_prefix('T4:')
    c_final_respondent = sheet.index_of('nome_inquirido_final')
    c_district = sheet.index_of('Selecione o Distrito do agregado')
    c_locality = sheet.index_of('Selecione a Localidade do agregado')
    c_consent = sheet.index_by_prefix('Autorizo a DPSSF')
    c_gps = {
        part: sheet.index_of(f'_B2: Coordenadas Geográficas_{part}')
        for part in ('latitude', 'longitude', 'altitude', 'precision')
    }
    answer_columns = sheet.answer_columns()

    households = {}
    for row in sheet.rows:
        index = as_text(row[c_index])
        nia = as_text(row[c_nia]).upper()
        if not index or not nia:
            raise CsuFileError(f"Agregado sem _index ou sem NIA validado (_index={index!r})")
        if index in households:
            raise CsuFileError(f"_index {index} repetido no ficheiro")
        interview_date = to_date(row[c_today])
        households[index] = Household(
            index=index,
            uuid=as_text(row[c_uuid]),
            nia=nia,
            nia_original=as_text(row[c_nia_original]).upper(),
            present=as_text(row[c_presence]).startswith(PRESENT),
            district=as_text(row[c_district]),
            respondent_name=as_text(row[c_final_respondent]) or as_text(row[c_respondent]),
            data={
                'index': index,
                'uuid': as_text(row[c_uuid]),
                'submission_id': as_text(row[c_id]),
                'nia_validado': nia,
                'nia_original': as_text(row[c_nia_original]).upper() or None,
                'data_entrevista': interview_date.isoformat() if interview_date else None,
                'inquiridor': as_text(row[c_interviewer]),
                'distrito': as_text(row[c_district]),
                'localidade': as_text(row[c_locality]),
                'consentimento_ficheiro': as_text(row[c_consent]) or None,
                'gps': {part: json_value(row[i]) for part, i in c_gps.items()},
                'respostas': _answers(row, answer_columns),
                'emigrantes': [],
            },
        )
    return households


def _apply_nia_corrections(households, corrections):
    for correction in corrections:
        if correction.type != CORRECTION_NIA:
            continue
        household = households.get(correction.index)
        if household is None:
            raise CsuFileError(f"Correcção de NIA para INDEX {correction.index} inexistente no CSU")
        if correction.previous_value and correction.previous_value.upper() != household.nia:
            raise CsuFileError(
                f"Correcção de NIA do INDEX {correction.index}: esperado {correction.previous_value}, "
                f"o ficheiro tem {household.nia}"
            )
        household.nia = correction.new_value.upper()
        household.data['nia_validado'] = household.nia
        household.data['nia_csu_validado'] = correction.previous_value.upper() or None
        household.corrections.append(correction)


def _check_unique_nias(households):
    seen = {}
    for household in households.values():
        other = seen.setdefault(household.nia, household.index)
        if other != household.index:
            raise CsuFileError(f"NIA {household.nia} atribuído aos INDEX {other} e {household.index}")


def _read_member_data(sheet, members_sheet):
    c_parent = sheet.index_of('_parent_index')
    c_pos = sheet.index_of('indice_membro_atual')
    c_relation = sheet.index_by_prefix('F1:')
    answer_columns = sheet.answer_columns()

    m_parent = members_sheet.index_of('_parent_index')
    m_pos = members_sheet.index_of('pos_memb')
    member_keys = {(as_text(r[m_parent]), as_text(r[m_pos])) for r in members_sheet.rows}

    data = {}
    orphans = 0
    for row in sheet.rows:
        key = (as_text(row[c_parent]), as_text(row[c_pos]))
        if key not in member_keys:
            orphans += 1
            continue
        data[key] = (as_text(row[c_relation]), _answers(row, answer_columns))
    return data, orphans


def _read_members(sheet, households, data_by_member):
    c_parent = sheet.index_of('_parent_index')
    c_pos = sheet.index_of('pos_memb')
    c_name = sheet.index_by_prefix('E1:')
    c_sex = sheet.index_by_prefix('E8:')
    c_bi = sheet.index_by_prefix('E6:')
    c_document_type = sheet.index_by_prefix('E5:')
    c_document_number = sheet.index_by_prefix('E6-a:')
    c_dob = sheet.index_by_prefix('E9:')
    c_age = sheet.index_of('idade_final')
    answer_columns = sheet.answer_columns()

    for row in sheet.rows:
        parent = as_text(row[c_parent])
        household = households.get(parent)
        if household is None:
            raise CsuFileError(f"Membro com _parent_index {parent} sem agregado")
        pos = as_text(row[c_pos])
        relation, extra_answers = data_by_member.get((parent, pos), ('', {}))
        sex = sex_code(row[c_sex])
        dob = to_date(row[c_dob])
        estimated = False
        if dob is None:
            reference = to_date(household.data['data_entrevista']) or date.today()
            dob = estimated_dob(row[c_age], reference)
            estimated = dob is not None
        if dob is None:
            raise CsuFileError(f"INDEX {parent}, membro {pos}: sem data de nascimento nem idade")

        document_number = as_text(row[c_document_number])
        if document_number in EMPTY_DOCUMENT_NUMBERS:
            document_number = ''
        full_name = re.sub(r'\s+', ' ', as_text(row[c_name]))
        first_name, last_name = split_name(full_name)
        answers = _answers(row, answer_columns)
        answers.update(extra_answers)
        answers['idade'] = json_value(row[c_age])

        household.members.append(Member(
            key=f"{household.uuid}:{pos}",
            pos=pos,
            full_name=full_name,
            first_name=first_name,
            last_name=last_name,
            sex=sex,
            dob=dob,
            dob_estimated=estimated,
            relation=relation,
            role=role_for(relation, sex),
            bi=as_text(row[c_bi]),
            document_type=as_text(row[c_document_type]),
            document_number=document_number,
            answers=answers,
        ))

    for household in households.values():
        household.members.sort(key=lambda m: (not m.is_head, int(m.pos) if m.pos.isdigit() else 0))


def _read_emigrants(sheet, households):
    c_parent = sheet.index_of('_parent_index')
    answer_columns = sheet.answer_columns()
    for row in sheet.rows:
        household = households.get(as_text(row[c_parent]))
        if household is not None:
            household.data['emigrantes'].append(_answers(row, answer_columns))


# ────────────────────────────────────────────────────────────
# Distritos (única função com acesso à base de dados)
# ────────────────────────────────────────────────────────────

def build_district_cache():
    """
    Chave normalizada do distrito no ficheiro ('MEZOCHI', 'RAP', ...) → Location.
    Compara sem acentos, hífenes nem espaços; os distritos ('D') têm prioridade sobre homónimos.
    """
    from location.models import Location

    by_key = {}
    for location in Location.objects.filter(validity_to__isnull=True):
        key = normalize_key(location.name)
        if key not in by_key or location.type == 'D':
            by_key[key] = location
    cache = {}
    for district, accepted in DISTRICT_ALIASES.items():
        for key in accepted:
            if key in by_key:
                cache[district] = by_key[key]
                break
    return cache


# ────────────────────────────────────────────────────────────
# Regras de inclusão
# ────────────────────────────────────────────────────────────

def is_no_members_exception(household):
    return any(c.type == CORRECTION_NO_MEMBERS_EXCEPTION for c in household.corrections)


def exclusion_reason(household):
    """
    Decisão DPSSF de 23/11: os agregados não entrevistados e os presentes sem membros ficam
    fora desta importação. Devolve o motivo, ou None se o agregado é importado.
    """
    if is_no_members_exception(household):
        return None
    if not household.present:
        return 'não entrevistado'
    if not household.members:
        return 'presente sem membros registados'
    if household.head is None:
        return 'sem chefe identificado'
    return None
