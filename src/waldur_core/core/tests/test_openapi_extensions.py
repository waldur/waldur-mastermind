import unittest

from rest_framework import serializers

from waldur_mastermind.marketplace import serializers as marketplace_serializers
from waldur_mastermind.proposal import serializers as proposal_serializers
from waldur_mastermind.support import serializers as support_serializers

# Model fields declared as `models.JSONField(default=list)` are picked up by
# ModelSerializer as a bare `serializers.JSONField`, which JSONFieldExtension
# maps to a free-form object. The published schema then claims `type: object`
# for a payload the API actually returns as an array, and strictly typed SDK
# clients fail to deserialize it. Each of these must therefore stay declared
# explicitly as a ListField.
LIST_VALUED_JSON_FIELDS = [
    (marketplace_serializers.ServiceProviderSerializer, "allowed_domains"),
    (marketplace_serializers.NestedSoftwareCatalogSerializer, "enabled_cpu_family"),
    (
        marketplace_serializers.NestedSoftwareCatalogSerializer,
        "enabled_cpu_microarchitectures",
    ),
    (marketplace_serializers.OfferingSoftwareCatalogSerializer, "enabled_cpu_family"),
    (
        marketplace_serializers.OfferingSoftwareCatalogSerializer,
        "enabled_cpu_microarchitectures",
    ),
    (
        marketplace_serializers.OfferingSoftwareCatalogUpdateSerializer,
        "enabled_cpu_family",
    ),
    (
        marketplace_serializers.OfferingSoftwareCatalogUpdateSerializer,
        "enabled_cpu_microarchitectures",
    ),
    (marketplace_serializers.NestedSoftwareTargetSerializer, "gpu_architectures"),
    (marketplace_serializers.SoftwareTargetSerializer, "gpu_architectures"),
    (support_serializers.ProviderSupportUserSerializer, "skills"),
    (proposal_serializers.ReviewerProfileSerializer, "alternative_names"),
    (proposal_serializers.ReviewerProfileCreateSerializer, "alternative_names"),
    (proposal_serializers.ReviewerPublicationSerializer, "coauthors"),
]


class ListValuedJSONFieldSchemaTest(unittest.TestCase):
    """
    Guards the schema type of list-valued JSON fields.

    `_declared_fields` is asserted rather than an instantiated serializer's
    `.fields` because several of these serializers need a request in their
    context, and the invariant is precisely that the field is *declared* rather
    than built from the model.
    """

    def test_list_valued_json_fields_are_declared_as_list_fields(self):
        for serializer_class, field_name in LIST_VALUED_JSON_FIELDS:
            with self.subTest(serializer=serializer_class.__name__, field=field_name):
                field = serializer_class._declared_fields.get(field_name)
                self.assertIsNotNone(
                    field,
                    f"{serializer_class.__name__}.{field_name} is not declared "
                    f"explicitly, so it will be built from the model as a "
                    f"JSONField and typed as an object in the schema.",
                )
                self.assertIsInstance(field, serializers.ListField)

    def test_software_target_gpu_architectures_stays_read_only(self):
        """
        SoftwareTargetSerializer.Meta sets `read_only_fields = fields`, but DRF
        applies that through extra_kwargs, which is never consulted for a
        declared field. The flag has to be set on the field itself.
        """
        field = marketplace_serializers.SoftwareTargetSerializer._declared_fields[
            "gpu_architectures"
        ]
        self.assertTrue(field.read_only)
