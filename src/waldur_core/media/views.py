import os

from django.http import Http404, HttpResponse
from django.utils.http import content_disposition_header
from rest_framework.views import APIView

from waldur_core.core.models import User

from . import models


def check_file_permissions(file: models.File, user: User):
    """
    If file is part of attachment, user should have appropriate permissions.
    """
    from waldur_mastermind.support.models import Attachment

    try:
        attachment = Attachment.objects.get(file=file.name)
    except Attachment.DoesNotExist:
        return
    if user.is_anonymous:
        raise Http404
    if not Attachment.objects.filter_for_user(user).filter(id=attachment.id).exists():
        raise Http404


class MediaView(APIView):
    permission_classes = ()

    def get(self, request, uuid):
        try:
            file = models.File.objects.get(uuid=uuid)
        except models.File.DoesNotExist:
            raise Http404
        check_file_permissions(file, request.user)
        filename = os.path.split(file.name)[-1]
        response = HttpResponse(file.content)
        response.headers["Content-Length"] = file.size
        response.headers["Content-Type"] = file.mime_type or "application/octet-stream"
        response.headers["Content-Disposition"] = content_disposition_header(
            as_attachment=True, filename=filename
        )
        return response
