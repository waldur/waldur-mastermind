from __future__ import unicode_literals

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse

from . import client


@login_required
def get_user_details(request):
    if not settings.WALDUR_AUTH_BCC['ENABLED']:
        return JsonResponse({'details': 'This feature is disabled'}, status=400)

    nid = request.GET.get('nid')
    vno = request.GET.get('vno')

    if not nid or not vno:
        return JsonResponse({'details': 'nid and vno are required parameters'}, status=400)

    try:
        user_details = client.get_user_details(nid, vno)
        return JsonResponse(user_details._asdict(), json_dumps_params={'ensure_ascii': False})
    except client.BCCException as e:
        return JsonResponse({'details': e.detail}, status=e.code)
