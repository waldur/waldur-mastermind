import os

from django.http import Http404, HttpResponse
from django.utils.http import content_disposition_header
from rest_framework.views import APIView

from . import models


class MediaView(APIView):
    authentication_classes = ()
    permission_classes = ()

    def get(self, request, uuid):
        try:
            file = models.File.objects.get(uuid=uuid)
        except models.File.DoesNotExist:
            raise Http404
        filename = os.path.split(file.name)[-1]
        response = HttpResponse(file.content)
        response.headers["Content-Length"] = file.size
        response.headers["Content-Type"] = file.mime_type or "application/octet-stream"
        response.headers["Content-Disposition"] = content_disposition_header(
            as_attachment=True, filename=filename
        )
        return response
