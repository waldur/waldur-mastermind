from rest_framework.test import APITransactionTestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import models as structure_tests_models
from waldur_mastermind.marketplace import handlers as marketplace_handlers

from .. import models as marketplace_models
from . import factories


class ResourceHandlerTest(APITransactionTestCase):
    def test_marketplace_resource_name_should_be_updated_if_resource_name_in_plugin_is_updated(
        self,
    ):
        marketplace_handlers.connect_resource_metadata_handlers(
            structure_tests_models.TestNewInstance
        )
        instance = structure_factories.TestNewInstanceFactory()
        resource = factories.ResourceFactory(scope=instance)
        instance.name = 'New name'
        instance.save()
        resource.refresh_from_db()
        self.assertEqual(resource.name, 'New name')

    def test_service_settings_should_be_disabled_if_resource_is_terminated(self,):
        marketplace_handlers.connect_resource_metadata_handlers(
            structure_tests_models.TestNewInstance
        )
        instance = structure_factories.TestNewInstanceFactory()
        resource: marketplace_models.Resource = factories.ResourceFactory(
            scope=instance
        )

        offering: marketplace_models.Offering = resource.offering
        service_settings = structure_factories.ServiceSettingsFactory()
        offering.scope = service_settings
        offering.archive()
        offering.save()

        service_settings.refresh_from_db()
        self.assertTrue(service_settings.is_active)

        resource.set_state_terminated()
        resource.save()

        service_settings.refresh_from_db()

        self.assertFalse(service_settings.is_active)

    def test_service_settings_should_be_disabled_if_offering_is_archived(self,):
        marketplace_handlers.connect_resource_metadata_handlers(
            structure_tests_models.TestNewInstance
        )
        instance = structure_factories.TestNewInstanceFactory()
        resource: marketplace_models.Resource = factories.ResourceFactory(
            scope=instance
        )

        offering: marketplace_models.Offering = resource.offering
        service_settings = structure_factories.ServiceSettingsFactory()
        offering.scope = service_settings
        offering.save()

        resource.set_state_terminated()
        resource.save()

        service_settings.refresh_from_db()
        self.assertTrue(service_settings.is_active)

        offering.archive()
        offering.save()

        service_settings.refresh_from_db()

        self.assertFalse(service_settings.is_active)

    def test_service_settings_should_be_ensabled_if_resource_is_not_terminated(self,):
        marketplace_handlers.connect_resource_metadata_handlers(
            structure_tests_models.TestNewInstance
        )
        instance = structure_factories.TestNewInstanceFactory()
        resource: marketplace_models.Resource = factories.ResourceFactory(
            scope=instance
        )

        offering: marketplace_models.Offering = resource.offering
        service_settings = structure_factories.ServiceSettingsFactory()
        service_settings.is_active = False
        service_settings.save()
        offering.scope = service_settings
        offering.save()

        resource.set_state_ok()
        resource.save()

        service_settings.refresh_from_db()
        self.assertTrue(service_settings.is_active)

    def test_service_settings_should_be_enabled_if_offering_is_not_archived(self,):
        marketplace_handlers.connect_resource_metadata_handlers(
            structure_tests_models.TestNewInstance
        )
        instance = structure_factories.TestNewInstanceFactory()
        resource: marketplace_models.Resource = factories.ResourceFactory(
            scope=instance
        )

        service_settings = structure_factories.ServiceSettingsFactory()
        service_settings.is_active = False
        service_settings.save()

        offering: marketplace_models.Offering = resource.offering
        offering.scope = service_settings
        offering.save()

        service_settings.refresh_from_db()
        self.assertFalse(service_settings.is_active)
