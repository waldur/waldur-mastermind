from django.utils.translation import ugettext_lazy as _

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import serializers as structure_serializers

from . import models


class ServiceSerializer(
    core_serializers.ExtraFieldOptionsMixin,
    core_serializers.RequiredFieldsMixin,
    structure_serializers.BaseServiceSerializer,
):
    SERVICE_ACCOUNT_FIELDS = {
        'backend_url': _('Waldur remote server URL'),
        'token': _('Waldur API access token'),
    }

    class Meta(structure_serializers.BaseServiceSerializer.Meta):
        model = models.RemoteWaldurService
        required_fields = ('backend_url', 'username', 'password', 'base_image_name')


class ServiceProjectLinkSerializer(
    structure_serializers.BaseServiceProjectLinkSerializer
):
    class Meta(structure_serializers.BaseServiceProjectLinkSerializer.Meta):
        model = models.RemoteWaldurServiceProjectLink
        extra_kwargs = {
            'service': {'lookup_field': 'uuid', 'view_name': 'remote-waldur-detail'},
        }
