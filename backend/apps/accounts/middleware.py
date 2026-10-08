from django.http import JsonResponse

from .security import user_must_change_password


class PasswordChangeRequiredMiddleware:
    """Keep temporary-password sessions out of the application until changed."""

    allowed_api_paths = frozenset({
        '/api/csrf/',
        '/api/login/',
        '/api/logout/',
        '/api/me/',
        '/api/password/change/',
        '/api/health/',
        '/api/ready/',
    })

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.path.startswith('/api/')
            and request.path not in self.allowed_api_paths
            and user_must_change_password(request.user)
        ):
            return JsonResponse({
                'detail': 'You must change your temporary password before continuing.',
                'code': 'password_change_required',
            }, status=403)
        return self.get_response(request)
