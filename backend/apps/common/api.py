from django.http import JsonResponse


def health(request):
    return JsonResponse(
        {
            "status": "ok",
            "service": "atlas-backend",
            "version": "0.1.0",
        }
    )
