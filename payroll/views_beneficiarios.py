"""
Views REST para gestão de beneficiários via HTTP (Postman / frontend).

Endpoints:
  POST /api/payroll/beneficiarios/backup/    — exporta backup JSON
  POST /api/payroll/beneficiarios/limpar/    — soft-delete com dry-run
  POST /api/payroll/beneficiarios/importar/  — importa Excel (multipart)
  POST /api/payroll/beneficiarios/restore/   — restaura backup JSON (multipart)

Todos os endpoints exigem autenticação JWT e utilizador staff (is_staff=True).
"""
import importlib.util
import json
import logging
import os
import tempfile
from io import StringIO

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

logger = logging.getLogger(__name__)

_COMMANDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'management', 'commands')


def _load_command(name):
    """Carrega um management command pelo caminho do ficheiro, sem depender do registry Django."""
    cmd_path = os.path.join(_COMMANDS_DIR, f'{name}.py')
    if not os.path.exists(cmd_path):
        raise FileNotFoundError(f"Comando não encontrado: {cmd_path}")
    spec = importlib.util.spec_from_file_location(name, cmd_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cmd = module.Command()
    out = StringIO()
    cmd.stdout = out
    cmd.stderr = StringIO()
    return cmd, out


def _require_staff(request):
    if not (request.user and request.user.is_staff):
        return Response(
            {'status': 'error', 'message': 'Acesso restrito a administradores (is_staff=True)'},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def beneficiarios_backup(request):
    """
    Gera um backup de todos os beneficiários activos.

    Body JSON (opcional):
      {
        "payroll_id": "<uuid>",        // filtrar BenefitConsumption por payroll
        "filename_prefix": "backup"    // prefixo do ficheiro (informativo)
      }

    Resposta: ficheiro JSON com todos os dados exportados.
    """
    deny = _require_staff(request)
    if deny:
        return deny

    data = request.data or {}
    payroll_id = data.get('payroll_id')

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd, out = _load_command('backup_beneficiarios')
            opts = {
                'output_dir': tmpdir,
                'filename_prefix': 'backup_beneficiarios',
                'payroll_id': payroll_id,
                'verbosity': 1, 'no_color': False, 'force_color': False,
            }
            cmd.handle(**opts)

            import glob as _glob
            files = _glob.glob(os.path.join(tmpdir, 'backup_beneficiarios_*.json'))
            if not files:
                return Response(
                    {'status': 'error', 'message': 'Ficheiro de backup não gerado'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            with open(files[0], 'r', encoding='utf-8') as f:
                backup_data = json.load(f)

        backup_data['_command_output'] = out.getvalue()
        return Response(backup_data, status=status.HTTP_200_OK)

    except Exception as exc:
        logger.exception("[beneficiarios_backup] Erro")
        return Response(
            {'status': 'error', 'message': str(exc)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def beneficiarios_limpar(request):
    """
    Soft-delete de todos os beneficiários activos.

    Body JSON:
      {
        "username": "admin",               // utilizador openIMIS (default: admin)
        "dry_run": true,                   // simular sem alterar (default: true)
        "force_skip_financial": false      // prosseguir com registos financeiros protegidos
      }

    ATENÇÃO: dry_run é true por default — passe explicitamente false para executar.
    """
    deny = _require_staff(request)
    if deny:
        return deny

    data = request.data or {}
    username = data.get('username', 'admin')
    dry_run = data.get('dry_run', True)   # default true para segurança
    force_skip = data.get('force_skip_financial', False)

    try:
        cmd, out = _load_command('limpar_beneficiarios')
        cmd.handle(
            username=username, dry_run=dry_run, force_skip_financial=force_skip,
            verbosity=1, no_color=False, force_color=False,
        )
        return Response({'status': 'ok', 'dry_run': dry_run, 'output': out.getvalue()},
                        status=status.HTTP_200_OK)

    except SystemExit as exc:
        return Response(
            {'status': 'error', 'message': str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as exc:
        logger.exception("[beneficiarios_limpar] Erro")
        return Response(
            {'status': 'error', 'message': str(exc)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def beneficiarios_importar(request):
    """
    Importa beneficiários a partir de um ficheiro Excel.

    Content-Type: multipart/form-data

    Campos do form:
      file              — ficheiro .xlsx (obrigatório)
      username          — utilizador openIMIS (default: admin)
      dry_run           — true/false (default: false)
      sheet             — nome da sheet (opcional)
      benefit_plan_id   — UUID do BenefitPlan (opcional — activa Fase 2)
      payroll_id        — UUID do Payroll (opcional — activa Fase 3)
      benefit_type      — tipo de benefício (default: CASH)
    """
    deny = _require_staff(request)
    if deny:
        return deny

    excel_file = request.FILES.get('file')
    if not excel_file:
        return Response(
            {'status': 'error', 'message': "Campo 'file' obrigatório (ficheiro .xlsx)"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    username = request.data.get('username', 'admin')
    dry_run = str(request.data.get('dry_run', 'false')).lower() in ('true', '1', 'yes')
    sheet = request.data.get('sheet') or None
    benefit_plan_id = request.data.get('benefit_plan_id') or None
    payroll_id = request.data.get('payroll_id') or None
    benefit_type = request.data.get('benefit_type', 'CASH')

    try:
        suffix = os.path.splitext(excel_file.name)[-1] or '.xlsx'
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            for chunk in excel_file.chunks():
                tmp.write(chunk)
            tmp_path = tmp.name

        try:
            cmd, out = _load_command('import_beneficiarios_excel')
            cmd.handle(
                excel_path=tmp_path,
                username=username,
                dry_run=dry_run,
                benefit_type=benefit_type,
                sheet=sheet,
                benefit_plan_id=benefit_plan_id,
                payroll_id=payroll_id,
                verbosity=1, no_color=False, force_color=False,
            )
            output = out.getvalue()
        finally:
            os.unlink(tmp_path)

        return Response({
            'status': 'ok',
            'dry_run': dry_run,
            'output': output,
        }, status=status.HTTP_200_OK)

    except SystemExit as exc:
        return Response(
            {'status': 'error', 'message': str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as exc:
        logger.exception("[beneficiarios_importar] Erro")
        return Response(
            {'status': 'error', 'message': str(exc)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def beneficiarios_restore(request):
    """
    Restaura beneficiários a partir de um ficheiro JSON de backup.

    Content-Type: multipart/form-data

    Campos do form:
      file          — ficheiro .json de backup (obrigatório)
      username      — utilizador openIMIS (default: admin)
      dry_run       — true/false (default: true)
      skip_phases   — fases a ignorar separadas por vírgula
                      (individuals, groups, groupindividuals, beneficiaries, consumptions)
    """
    deny = _require_staff(request)
    if deny:
        return deny

    backup_file = request.FILES.get('file')
    if not backup_file:
        return Response(
            {'status': 'error', 'message': "Campo 'file' obrigatório (ficheiro .json de backup)"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    username = request.data.get('username', 'admin')
    dry_run = str(request.data.get('dry_run', 'true')).lower() in ('true', '1', 'yes')
    skip_phases_raw = request.data.get('skip_phases', '')
    skip_phases = [p.strip() for p in skip_phases_raw.split(',') if p.strip()] if skip_phases_raw else []

    try:
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as tmp:
            for chunk in backup_file.chunks():
                tmp.write(chunk)
            tmp_path = tmp.name

        try:
            out = StringIO()
            cmd, out = _load_command('restore_beneficiarios')
            cmd.handle(
                backup_file=tmp_path,
                username=username,
                dry_run=dry_run,
                skip_phases=skip_phases,
                verbosity=1, no_color=False, force_color=False,
            )
            output = out.getvalue()
        finally:
            os.unlink(tmp_path)

        return Response({
            'status': 'ok',
            'dry_run': dry_run,
            'output': output,
        }, status=status.HTTP_200_OK)

    except SystemExit as exc:
        return Response(
            {'status': 'error', 'message': str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as exc:
        logger.exception("[beneficiarios_restore] Erro")
        return Response(
            {'status': 'error', 'message': str(exc)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
