from django.http.response import Http404
from rest_framework.views import APIView

from waldur_core.media.utils import send_file, get_file_from_token


class ProtectedFileView(APIView):
    authentication_classes = ()
    permission_classes = ()

    def get(self, request, token):
        value = get_file_from_token(token)
        if not value:
            raise Http404
        return send_file(value)
