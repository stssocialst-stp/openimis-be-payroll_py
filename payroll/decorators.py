import functools
import os

from rest_framework.response import Response


def bistp_callback_auth(view_func):
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        auth = request.headers.get('Authorization', '')
        expected = os.environ.get('BISTP_CALLBACK_TOKEN', '')
        if not expected or auth != f'Bearer {expected}':
            return Response({"status": "error", "message": "Unauthorized"}, status=401)
        return view_func(request, *args, **kwargs)
    return wrapper
