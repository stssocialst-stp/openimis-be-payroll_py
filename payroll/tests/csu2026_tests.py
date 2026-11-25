import datetime
import os
import tempfile

from django.test import SimpleTestCase

from payroll import csu2026

HOUSEHOLD_HEADERS = [
    'today', 'nome_inquiridor', 'R2: Assinatura do Inquiridor', 'R4: Presença do agregado',
    'Autorizo a DPSSF a utilizar os meus dados pessoais', 'T2: Escrever o NIA.',
    'T4: Escreva o nome do entrevistado', 'nome_inquirido_final',
    'Selecione o Distrito do agregado', 'Selecione a Localidade do agregado',
    '_B2: Coordenadas Geográficas_latitude', '_B2: Coordenadas Geográficas_longitude',
    '_B2: Coordenadas Geográficas_altitude', '_B2: Coordenadas Geográficas_precision',
    'C1: Tipo de alojamento', 'C12: Combustíveis?', 'C12: Combustíveis?/Lenha', 'D12: Telemóveis',
    '_id', '_uuid', '_index', 'NIA_AGREGADO_VALIDADO', 'NIA_ORIGINAL_CSU',
]
MEMBER_HEADERS = [
    'pos_memb', 'E1: Nome completo', 'E5: Outro documento', 'E6: Número do BI', 'E6-a: Número do documento',
    'E7: Fotografia', 'E7: Fotografia_URL', 'E8: Sexo', 'E9: Data de nascimento', 'idade_final',
    '_parent_index',
]
MEMBER_DATA_HEADERS = [
    'nome_completo', 'indice_membro_atual', 'F1: Parentesco', 'F10-b): Nome da escola',
    'F8: Nacionalidade?', 'F8: Nacionalidade?/Angolana', '_parent_index',
]
EMIGRANT_HEADERS = ['G6: Nome completo', 'G9: Residência', '_parent_index']

INTERVIEW = datetime.datetime(2026, 3, 30)


def household(index, nia, original=None, present='1. Está presente', district='MÉ-ZÓCHI'):
    return [INTERVIEW, 'Inquiridor', 'assinatura.jpg', present, 'consent.jpg', nia, 'Entrevistado',
            'Ana Lima', district, 'Bombom', 0.3, 6.7, 10, 5, 'Casa isolada', '1. Lenha', 1, 2,
            1000 + index, f'uuid-{index}', index, nia, original]


def member(parent, pos, name, sex='2. Feminino', bi=None, dob=None, age='30', doc_type=None, doc_number=None):
    return [str(pos), name, doc_type, bi, doc_number, 'foto.jpg', 'https://kobo/foto', sex, dob, age, parent]


def member_data(parent, pos, name, relation):
    return [name, str(pos), relation, 'Escola X', 'São-tomense', 0, parent]


class Csu2026FileTest(SimpleTestCase):

    def _write(self, households, members, data, emigrants=()):
        import openpyxl

        workbook = openpyxl.Workbook()
        sheets = (
            (csu2026.SHEET_HOUSEHOLDS, HOUSEHOLD_HEADERS, households),
            (csu2026.SHEET_MEMBERS, MEMBER_HEADERS, members),
            (csu2026.SHEET_MEMBER_DATA, MEMBER_DATA_HEADERS, data),
            (csu2026.SHEET_EMIGRANTS, EMIGRANT_HEADERS, emigrants),
        )
        workbook.remove(workbook.active)
        for name, headers, rows in sheets:
            sheet = workbook.create_sheet(name)
            sheet.append(headers)
            for row in rows:
                sheet.append(row)
        handle, path = tempfile.mkstemp(suffix='.xlsx')
        os.close(handle)
        workbook.save(path)
        self.addCleanup(os.remove, path)
        return path

    def _corrections(self, text):
        handle, path = tempfile.mkstemp(suffix='.csv')
        with os.fdopen(handle, 'w', encoding='utf-8') as out:
            out.write(text)
        self.addCleanup(os.remove, path)
        return csu2026.load_corrections(path)

    def _basic_file(self):
        return self._write(
            households=[household(1, 'AB1CD2'), household(2, 'EF3GH4', original='EF3GH5'),
                        household(3, 'IJ5KL6', present='3. Em mudança / em viagem'), household(4, 'MN7OP8')],
            members=[
                member(1, 2, 'Rui  Lima', sex='1. Masculino', dob=None, age='8',
                       doc_type='Cédula Pessoal', doc_number='000'),
                member(1, 1, 'Ana Lima', bi=123456, dob=datetime.datetime(1990, 5, 1)),
                member(2, 1, 'Eva Costa', bi=123456, dob=datetime.datetime(1985, 1, 1)),
            ],
            data=[
                member_data(1, 1, 'Ana Lima', 'CHEFE do agregado famíliar'),
                member_data(1, 2, 'Rui Lima', 'FILHO(A) do chefe do agregado famíliar'),
                member_data(2, 1, 'Eva Costa', 'CHEFE do agregado famíliar'),
                member_data(9, 1, 'Órfão', 'CHEFE do agregado famíliar'),
            ],
            emigrants=[['Zé Lima', 'Portugal', 1]],
        )

    def test_households_members_and_links(self):
        result = csu2026.read_csu_file(self._basic_file())
        self.assertEqual(len(result.households), 4)
        self.assertEqual(result.orphan_member_data, 1)

        first = result.households['1']
        self.assertEqual(first.nia, 'AB1CD2')
        self.assertEqual([m.full_name for m in first.members], ['Ana Lima', 'Rui Lima'])
        head, child = first.members
        self.assertEqual(head.role, 'HEAD')
        self.assertEqual(head.key, 'uuid-1:1')
        self.assertEqual(head.bi, '123456')
        self.assertEqual(child.role, 'SON')
        self.assertEqual(child.sex, 'M')
        self.assertEqual(first.data['emigrantes'], [{'G6': 'Zé Lima', 'G9': 'Portugal'}])
        self.assertEqual(first.data['gps']['latitude'], 0.3)

    def test_estimated_dob_and_placeholder_document(self):
        child = csu2026.read_csu_file(self._basic_file()).households['1'].members[1]
        self.assertTrue(child.dob_estimated)
        self.assertEqual(child.dob, datetime.date(2018, 1, 1))
        self.assertEqual(child.document_number, '')
        self.assertEqual(child.document_type, 'Cédula Pessoal')

    def test_answers_are_keyed_by_question_code(self):
        result = csu2026.read_csu_file(self._basic_file())
        answers = result.households['1'].data['respostas']
        self.assertEqual(answers['C1'], 'Casa isolada')
        self.assertEqual(answers['C12'], '1. Lenha')
        self.assertNotIn('R2', answers)
        member_answers = result.households['1'].members[0].answers
        self.assertEqual(member_answers['F10-b'], 'Escola X')
        self.assertEqual(member_answers['E7_url'], 'https://kobo/foto')
        self.assertEqual(member_answers['F8'], 'São-tomense')

    def test_exclusion_rules(self):
        households = csu2026.read_csu_file(self._basic_file()).households
        self.assertIsNone(csu2026.exclusion_reason(households['1']))
        self.assertEqual(csu2026.exclusion_reason(households['3']), 'não entrevistado')
        self.assertEqual(csu2026.exclusion_reason(households['4']), 'presente sem membros registados')

    def test_corrections_swap_nias_and_mark_exception(self):
        corrections = self._corrections(
            "tipo,index,valor_anterior,valor_novo,referencia\n"
            "NIA,1,AB1CD2,MN7OP8,email\n"
            "NIA,4,MN7OP8,AB1CD2,email\n"
            "EXCECAO_SEM_MEMBROS,4,,,email\n"
            "INDEX_LISTA,5490,,110,email\n"
        )
        households = csu2026.read_csu_file(self._basic_file(), corrections).households
        self.assertEqual(households['1'].nia, 'MN7OP8')
        self.assertEqual(households['4'].nia, 'AB1CD2')
        self.assertTrue(csu2026.is_no_members_exception(households['4']))
        self.assertIsNone(csu2026.exclusion_reason(households['4']))
        self.assertEqual(csu2026.list_index_corrections(corrections)['5490'].new_value, '110')

    def test_correction_that_duplicates_a_nia_is_rejected(self):
        corrections = self._corrections(
            "tipo;index;valor_anterior;valor_novo;referencia\nNIA;1;AB1CD2;EF3GH4;email\n"
        )
        with self.assertRaises(csu2026.CsuFileError):
            csu2026.read_csu_file(self._basic_file(), corrections)

    def test_correction_with_wrong_previous_value_is_rejected(self):
        corrections = self._corrections(
            "tipo,index,valor_anterior,valor_novo,referencia\nNIA,1,ZZ9ZZ9,QQ1QQ1,email\n"
        )
        with self.assertRaises(csu2026.CsuFileError):
            csu2026.read_csu_file(self._basic_file(), corrections)


class Csu2026MappingTest(SimpleTestCase):

    def test_roles_follow_relation_and_sex(self):
        cases = {
            ('CHEFE do agregado famíliar', 'F'): 'HEAD',
            ('CÔNJUGE ou Parceiro(a) do chefe', 'M'): 'SPOUSE',
            ('FILHO(A) do chefe', 'F'): 'DAUGHTER',
            ('NETO(A) do chefe', 'M'): 'GRANDSON',
            ('PAI/MÃE do chefe', 'F'): 'MOTHER',
            ('IRMÃO(Ã) do chefe', 'M'): 'BROTHER',
            ('AVÔ/AVÓ do chefe', 'F'): 'GRANDMOTHER',
            ('ENTEADO(A) do chefe', 'M'): 'OTHER RELATIVE',
            ('Genro/Nora do chefe', 'F'): 'OTHER RELATIVE',
            ('DOMÉSTICA OU PARENTE DOMÉSTICA', 'F'): 'OTHER RELATIVE',
            ('OUTRO sem parentesco', 'M'): 'NOT RELATED',
        }
        for (relation, sex), role in cases.items():
            self.assertEqual(csu2026.role_for(relation, sex), role, relation)

    def test_district_keys(self):
        self.assertEqual(csu2026.normalize_key('MÉ-ZÓCHI'), 'MEZOCHI')
        self.assertEqual(csu2026.normalize_key('Água Grande'), 'AGUAGRANDE')
        self.assertIn('PAGUE', csu2026.DISTRICT_ALIASES['RAP'])

    def test_estimated_dob_bounds(self):
        reference = datetime.date(2026, 3, 30)
        self.assertEqual(csu2026.estimated_dob('0', reference), datetime.date(2026, 1, 1))
        self.assertIsNone(csu2026.estimated_dob('abc', reference))
        self.assertIsNone(csu2026.estimated_dob('200', reference))

    def test_names(self):
        self.assertEqual(csu2026.normalize_name(' Maria  livramento Mendonça '), 'MARIA LIVRAMENTO MENDONCA')
        self.assertEqual(csu2026.split_name('Ana Maria Lima'), ('Ana', 'Maria Lima'))


class Csu2026MemberMatchingTest(SimpleTestCase):
    """Correspondência dos membros do CSU com os Individuals já existentes no agregado."""

    def _member(self, pos, name, bi=''):
        first, last = csu2026.split_name(name)
        return csu2026.Member(
            key=f"uuid-1:{pos}", pos=str(pos), full_name=name, first_name=first, last_name=last,
            sex='F', dob=datetime.date(1990, 1, 1), dob_estimated=False, relation='', role='HEAD' if pos == 1 else 'DAUGHTER',
            bi=bi, document_type='', document_number='',
        )

    def _existing(self, ind_id, name, bi=None, key=None):
        from types import SimpleNamespace

        first, last = csu2026.split_name(name)
        json_ext = {}
        if bi:
            json_ext['bilhete_didentidade'] = bi
        if key:
            json_ext['csu2026'] = {'member_key': key}
        individual = SimpleNamespace(id=ind_id, first_name=first, last_name=last, json_ext=json_ext, is_deleted=False)
        return SimpleNamespace(individual=individual, individual_id=ind_id)

    def _match(self, members, existing):
        from payroll.management.commands.import_csu_2026 import Command

        household = csu2026.Household(index='1', uuid='uuid-1', nia='AB1CD2', nia_original='', present=True,
                                      district='', data={}, members=members)
        return {key: (ind.id if ind else None) for key, ind in Command()._match_members(household, existing).items()}

    def test_key_then_bi_then_name(self):
        members = [self._member(1, 'Ana Lima', bi='111'), self._member(2, 'Eva Lima'), self._member(3, 'Rita Lima')]
        existing = [
            self._existing('a', 'ANA LIMA', bi=111),
            self._existing('e', 'Eva  Lima'),
            self._existing('r', 'Outro Nome', key='uuid-1:3'),
        ]
        self.assertEqual(self._match(members, existing), {'uuid-1:1': 'a', 'uuid-1:2': 'e', 'uuid-1:3': 'r'})

    def test_name_with_contradicting_bi_is_not_matched(self):
        members = [self._member(1, 'Ana Lima', bi='111')]
        existing = [self._existing('a', 'Ana Lima', bi='222')]
        self.assertEqual(self._match(members, existing), {'uuid-1:1': None})

    def test_each_individual_is_used_once(self):
        members = [self._member(1, 'Ana Lima'), self._member(2, 'Ana Lima')]
        existing = [self._existing('a', 'Ana Lima')]
        self.assertEqual(self._match(members, existing), {'uuid-1:1': 'a', 'uuid-1:2': None})
