"""
Views REST para gestão de beneficiários via HTTP (Postman / frontend).

Todos os endpoints são ASSÍNCRONOS: devolvem clientMutationId imediatamente,
a operação corre em background via Celery. O frontend faz poll com:

  query { mutationLogs(clientMutationId: "<id>") { edges { node { status error jsonExt } } } }

Status do MutationLog:
  0 = PENDING (a processar)
  1 = ERROR   (falhou — ver campo error)
  2 = SUCCESS (concluído — ver campo jsonExt.output ou jsonExt.backup_file)

Endpoints:
  POST /api/payroll/beneficiarios/backup/    — exporta backup JSON
  POST /api/payroll/beneficiarios/limpar/    — soft-delete com dry-run
  POST /api/payroll/beneficiarios/importar/  — importa Excel (multipart)
  POST /api/payroll/beneficiarios/restore/   — restaura backup JSON (multipart)
  GET  /api/payroll/beneficiarios/backup/<client_mutation_id>/download/  — descarrega ficheiro
"""
import logging
import os
import tempfile
import uuid

from django.http import FileResponse, Http404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from payroll.tasks import (
    backup_beneficiarios_task,
    limpar_beneficiarios_task,
    importar_beneficiarios_task,
    restore_beneficiarios_task,
    BACKUP_DIR,
)

logger = logging.getLogger(__name__)


def _require_staff(request):
    if not (request.user and request.user.is_staff):
        return Response(
            {'status': 'error', 'message': 'Acesso restrito a administradores (is_staff=True)'},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


def _create_mutation_log(label, details=None):
    """Cria MutationLog com status PENDING e devolve o registo."""
    from core.models import MutationLog
    client_mutation_id = str(uuid.uuid4())
    log = MutationLog(
        client_mutation_id=client_mutation_id,
        client_mutation_label=label,
        client_mutation_details=details,
        status=0,
    )
    log.save()
    return log


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def beneficiarios_backup(request):
    """
    Inicia backup assíncrono de todos os beneficiários activos.

    Body JSON (opcional):
      { "payroll_id": "<uuid>" }

    Resposta imediata:
      { "clientMutationId": "...", "status": "pending" }

    Após conclusão (poll em mutationLogs):
      jsonExt.backup_file — nome do ficheiro gerado
    Descarregar:
      GET /api/payroll/beneficiarios/backup/<clientMutationId>/download/
    """
    deny = _require_staff(request)
    if deny:
        return deny

    data = request.data or {}
    payroll_id = data.get('payroll_id')

    log = _create_mutation_log('Backup Beneficiários')
    backup_beneficiarios_task.delay(log.client_mutation_id, payroll_id, request.user.username)

    return Response({
        'clientMutationId': log.client_mutation_id,
        'status': 'pending',
        'message': 'Backup iniciado em background. Monitorize via mutationLogs.',
    }, status=status.HTTP_202_ACCEPTED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def beneficiarios_backup_download(request, client_mutation_id):
    """
    Descarrega o ficheiro de backup gerado pelo task identificado por clientMutationId.
    Só funciona após o MutationLog estar em status=2 (SUCCESS).
    """
    deny = _require_staff(request)
    if deny:
        return deny

    from core.models import MutationLog
    log = MutationLog.objects.filter(client_mutation_id=client_mutation_id).first()
    if not log:
        raise Http404("MutationLog não encontrado")

    if log.status == 0:
        return Response({'status': 'pending', 'message': 'Backup ainda em processamento'},
                        status=status.HTTP_202_ACCEPTED)
    if log.status == 1:
        return Response({'status': 'error', 'message': log.error},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    backup_file = (log.json_ext or {}).get('backup_file')
    if not backup_file:
        raise Http404("Ficheiro de backup não encontrado no MutationLog")

    filepath = os.path.join(BACKUP_DIR, backup_file)
    if not os.path.exists(filepath):
        raise Http404(f"Ficheiro não encontrado no servidor: {backup_file}")

    return FileResponse(
        open(filepath, 'rb'),
        as_attachment=True,
        filename=backup_file,
        content_type='application/json',
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def beneficiarios_limpar(request):
    """
    Inicia soft-delete assíncrono de todos os beneficiários activos.

    Body JSON:
      {
        "username": "admin",
        "dry_run": true,
        "force_skip_financial": false
      }

    Resposta imediata:
      { "clientMutationId": "...", "status": "pending" }

    Após conclusão (poll em mutationLogs):
      jsonExt.output — log do comando
    """
    deny = _require_staff(request)
    if deny:
        return deny

    data = request.data or {}
    username = data.get('username', 'admin')
    dry_run = data.get('dry_run', True)
    force_skip = data.get('force_skip_financial', False)

    label = 'Limpar Beneficiários (dry-run)' if dry_run else 'Limpar Beneficiários'
    log = _create_mutation_log(label)
    limpar_beneficiarios_task.delay(log.client_mutation_id, username, dry_run, force_skip)

    return Response({
        'clientMutationId': log.client_mutation_id,
        'status': 'pending',
        'dry_run': dry_run,
        'message': 'Limpeza iniciada em background. Monitorize via mutationLogs.',
    }, status=status.HTTP_202_ACCEPTED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def beneficiarios_importar(request):
    """
    Inicia importação assíncrona de beneficiários a partir de Excel.

    Content-Type: multipart/form-data

    Campos do form:
      file              — ficheiro .xlsx (obrigatório)
      username          — utilizador openIMIS (default: admin)
      dry_run           — true/false (default: false)
      sheet             — nome da sheet (opcional)
      benefit_plan_id   — UUID do BenefitPlan (opcional)
      payroll_id        — UUID do Payroll (opcional)
      benefit_type      — tipo de benefício (default: CASH)

    Resposta imediata:
      { "clientMutationId": "...", "status": "pending" }

    Após conclusão (poll em mutationLogs):
      jsonExt.output — log do comando com estatísticas
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

    # Guardar ficheiro em localização persistente (não apagar antes do task correr)
    suffix = os.path.splitext(excel_file.name)[-1] or '.xlsx'
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, prefix='kenon_import_')
    try:
        for chunk in excel_file.chunks():
            tmp.write(chunk)
        tmp_path = tmp.name
    finally:
        tmp.close()

    label = 'Importar Beneficiários (dry-run)' if dry_run else 'Importar Beneficiários'
    log = _create_mutation_log(label, details=excel_file.name)
    importar_beneficiarios_task.delay(
        log.client_mutation_id, tmp_path, username,
        dry_run, sheet, benefit_plan_id, payroll_id, benefit_type,
    )

    return Response({
        'clientMutationId': log.client_mutation_id,
        'status': 'pending',
        'dry_run': dry_run,
        'message': 'Importação iniciada em background. Monitorize via mutationLogs.',
    }, status=status.HTTP_202_ACCEPTED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def beneficiarios_restore(request):
    """
    Inicia restauro assíncrono a partir de ficheiro JSON de backup.

    Content-Type: multipart/form-data

    Campos do form:
      file          — ficheiro .json de backup (obrigatório)
      username      — utilizador openIMIS (default: admin)
      dry_run       — true/false (default: true)
      skip_phases   — fases a ignorar separadas por vírgula

    Resposta imediata:
      { "clientMutationId": "...", "status": "pending" }

    Após conclusão (poll em mutationLogs):
      jsonExt.output — log do comando
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

    tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False, prefix='kenon_restore_')
    try:
        for chunk in backup_file.chunks():
            tmp.write(chunk)
        tmp_path = tmp.name
    finally:
        tmp.close()

    label = 'Restore Beneficiários (dry-run)' if dry_run else 'Restore Beneficiários'
    log = _create_mutation_log(label)
    restore_beneficiarios_task.delay(log.client_mutation_id, tmp_path, username, dry_run, skip_phases)

    return Response({
        'clientMutationId': log.client_mutation_id,
        'status': 'pending',
        'dry_run': dry_run,
        'message': 'Restore iniciado em background. Monitorize via mutationLogs.',
    }, status=status.HTTP_202_ACCEPTED)
