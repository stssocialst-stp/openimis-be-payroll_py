from django.urls import path

from payroll.views import (
    bistp_payment_status_callback,
    send_callback_to_openimis,
    CSVReconciliationAPIView,
    bistp_account_info,
)

urlpatterns = [
    path('send_callback_to_openimis/', send_callback_to_openimis),
    path('bistp/payments/status/', bistp_payment_status_callback),
    path('bistp/account/info/', bistp_account_info),
    path('csv_reconciliation/', CSVReconciliationAPIView.as_view()),
]
