from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import apps


class VolumeMetadataCreationTest(test.APITestCase):
    def test_volume_metadata_is_populated_on_resource_creation(self):
        offering = marketplace_factories.OfferingFactory(
            type=apps.OPENSTACK_VOLUME_OFFERING
        )
        attributes = {"size": 10240}

        resource = marketplace_models.Resource.objects.create(
            offering=offering,
            project=structure_factories.ProjectFactory(),
            attributes=attributes,
            name="Test Volume",
        )

        resource.refresh_from_db()
        self.assertEqual(resource.backend_metadata.get("size"), 10240)

    def test_volume_metadata_is_not_populated_if_type_mismatch(self):
        offering = marketplace_factories.OfferingFactory(type="Other.Type")
        attributes = {"size": 10240}

        resource = marketplace_models.Resource.objects.create(
            offering=offering,
            project=structure_factories.ProjectFactory(),
            attributes=attributes,
            name="Test Other Resource",
        )

        resource.refresh_from_db()
        self.assertIsNone(resource.backend_metadata.get("size"))
