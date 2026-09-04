from django.urls import path

from payroll.views import (
    bistp_payment_status_callback,
    send_callback_to_openimis,
    CSVReconciliationAPIView,
    bistp_account_info,
)
from payroll.views_beneficiarios import (
    beneficiarios_backup,
    beneficiarios_backup_download,
    beneficiarios_limpar,
    beneficiarios_importar,
    beneficiarios_restore,
)

urlpatterns = [
    path('send_callback_to_openimis/', send_callback_to_openimis),
    path('bistp/payments/status/', bistp_payment_status_callback),
    path('bistp/account/info/', bistp_account_info),
    path('csv_reconciliation/', CSVReconciliationAPIView.as_view()),
    path('beneficiarios/backup/', beneficiarios_backup),
    path('beneficiarios/backup/<str:client_mutation_id>/download/', beneficiarios_backup_download),
    path('beneficiarios/limpar/', beneficiarios_limpar),
    path('beneficiarios/importar/', beneficiarios_importar),
    path('beneficiarios/restore/', beneficiarios_restore),
]
