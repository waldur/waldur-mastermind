from rest_framework.test import APITransactionTestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import handlers as marketplace_handlers
from waldur_core.structure.tests import models as structure_tests_models

from . import factories


class ResourceHandlerTest(APITransactionTestCase):
    def test_marketplace_resource_name_should_be_updated_if_resource_name_in_plugin_is_updated(self):
        marketplace_handlers.connect_resource_metadata_handlers(structure_tests_models.TestNewInstance)
        instance = structure_factories.TestNewInstanceFactory()
        resource = factories.ResourceFactory(scope=instance)
        instance.name = 'New name'
        instance.save()
        resource.refresh_from_db()
        self.assertEqual(resource.name, 'New name')
