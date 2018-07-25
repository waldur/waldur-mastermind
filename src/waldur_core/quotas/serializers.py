from rest_framework import serializers

from waldur_core.core.serializers import GenericRelatedField
from waldur_core.quotas import models, utils


class QuotaSerializer(serializers.HyperlinkedModelSerializer):
    scope = GenericRelatedField(related_models=utils.get_models_with_quotas(), read_only=True)

    class Meta(object):
        model = models.Quota
        fields = ('url', 'uuid', 'name', 'limit', 'usage', 'scope', 'threshold')
        read_only_fields = ('uuid', 'name', 'usage')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class BasicQuotaSerializer(serializers.HyperlinkedModelSerializer):
    """
    It does not expose scope in order to reduce number of queries
    """
    class Meta(object):
        model = models.Quota
        fields = ('url', 'uuid', 'name', 'limit', 'usage')
        read_only_fields = ('uuid', 'name', 'usage')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }
