import os

from django.http import Http404, HttpResponse
from django.utils.http import content_disposition_header
from drf_spectacular.utils import extend_schema
from rest_framework.generics import GenericAPIView

from waldur_core.core.models import User

from . import access, models
from .utils import MARKDOWN_IMAGE_PREFIX


def check_file_permissions(file: models.File, user: User):
    """Deny access unless an app has registered a rule that allows it.

    Rules live in each app's ``media_access`` module; see
    :mod:`waldur_core.media.access` for the registry and the default-deny
    contract.
    """
    if not access.user_can_access_file(file, user):
        raise Http404


def _serve_inline(file: models.File) -> bool:
    if file.name.startswith(MARKDOWN_IMAGE_PREFIX):
        return True
    return bool(file.mime_type and file.mime_type.startswith("image/"))


class MediaView(GenericAPIView):
    permission_classes = ()
    filter_backends = ()

    @extend_schema(
        description="Get media file",
        responses={200: bytes},
    )
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
            as_attachment=not _serve_inline(file),
            filename=filename,
        )
        return response
