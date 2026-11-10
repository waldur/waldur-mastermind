"""Pause the API while a demo preset loads.

A load truncates most of the database, so requests that reach it meanwhile
fail in confusing ways or write rows the load then wipes. While a load is
pending or running, every /api/ request gets 503 with ``demo_reset: true``,
except those a browser needs to follow the load and log in again afterwards.
"""

from django.http import JsonResponse

from waldur_mastermind.marketplace.demo_presets import gate, jobs

STATUS_PATH = "/api/demo-preset-status/"
EXEMPT_PREFIXES = (STATUS_PATH, "/api/configuration/", "/api-auth/")
RETRY_AFTER_SECONDS = 10


def demo_preset_reset_middleware(get_response):
    def middleware(request):
        if not gate.is_enabled():
            return get_response(request)
        path = request.path
        if not path.startswith("/api/") or path.startswith(EXEMPT_PREFIXES):
            return get_response(request)
        load = jobs.active_load()
        if load is None:
            return get_response(request)
        response = JsonResponse(
            {
                "demo_reset": True,
                "preset": load.preset,
                "detail": "A demo preset is loading. Try again shortly.",
            },
            status=503,
        )
        response["Retry-After"] = str(RETRY_AFTER_SECONDS)
        return response

    return middleware
