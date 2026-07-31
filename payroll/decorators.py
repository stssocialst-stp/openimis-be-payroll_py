import functools
import logging

from rest_framework.response import Response

logger = logging.getLogger(__name__)


def bistp_callback_auth(view_func):
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        from oauth2_provider.models import AccessToken
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            logger.warning("[BISTP][Callback] Pedido sem token Bearer — rejeitado")
            return Response({"status": "error", "message": "Unauthorized"}, status=401)
        token_str = auth[7:].strip()
        try:
            token = AccessToken.objects.select_related('application').get(token=token_str)
            if not token.is_valid():
                logger.warning("[BISTP][Callback] Token expirado ou revogado")
                return Response({"status": "error", "message": "Unauthorized"}, status=401)
            logger.debug("[BISTP][Callback] Token válido — application=%s",
                         token.application.name if token.application else "?")
            return view_func(request, *args, **kwargs)
        except AccessToken.DoesNotExist:
            logger.warning("[BISTP][Callback] Token não encontrado")
            return Response({"status": "error", "message": "Unauthorized"}, status=401)
    return wrapper
