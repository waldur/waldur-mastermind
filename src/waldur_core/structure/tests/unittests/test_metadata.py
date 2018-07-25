from django.test import TestCase
from rest_framework import serializers

from waldur_core.structure.metadata import ActionsMetadata


class ResourceProvisioningMetadataTest(TestCase):
    def get_serializer(self):
        STATE_CHOICES = (
            (1, 'Ready'),
            (2, 'Erred')
        )

        class Queryset(object):
            def all(self):
                return {'key': 'value'}

            def __iter__(self):
                return iter([])

        class VirtualMachineSerializer(serializers.Serializer):
            name = serializers.CharField(max_length=100, read_only=True)
            description = serializers.CharField(max_length=100)

            state = serializers.ChoiceField(choices=STATE_CHOICES)
            image = serializers.RelatedField(queryset=Queryset())

        return VirtualMachineSerializer()

    def test_read_only_options_are_skipped(self):
        options = ActionsMetadata()

        serializer = self.get_serializer()
        serializer_info = options.get_serializer_info(serializer)

        self.assertIn('description', serializer_info)
        self.assertNotIn('name', serializer_info)

    def test_choices_for_related_fields_are_not_exposed(self):
        options = ActionsMetadata()

        serializer = self.get_serializer()
        serializer_info = options.get_serializer_info(serializer)

        self.assertIn('choices', serializer_info['state'])
        self.assertNotIn('choices', serializer_info['image'])
