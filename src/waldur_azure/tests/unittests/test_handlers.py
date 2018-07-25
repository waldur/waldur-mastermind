from django.test import TestCase

from .. import factories


class SecurityGroupHandlerTest(TestCase):

    def test_cloud_service_name_is_copied_from_provider_settings_on_spl_creation(self):
        settings = factories.AzureServiceSettingsFactory()
        settings.options = {'cloud_service_name': 'azure-cloud'}
        settings.save()
        azure_service = factories.AzureServiceFactory(settings=settings)

        spl = factories.AzureServiceProjectLinkFactory(service=azure_service)

        self.assertEquals(spl.cloud_service_name, settings.options['cloud_service_name'])

    def test_cloud_service_name_is_not_populated_if_it_is_empty_in_settings(self):
        spl = factories.AzureServiceProjectLinkFactory()

        self.assertEqual('', spl.cloud_service_name)

    def test_cloud_service_name_is_not_overwritten_on_creation_if_it_is_passed_explicitely(self):
        settings = factories.AzureServiceSettingsFactory()
        spl_cloud_name = 'spl-cloud'
        settings.options = {'cloud_service_name': 'azure-cloud'}
        settings.save()
        azure_service = factories.AzureServiceFactory(settings=settings)

        spl = factories.AzureServiceProjectLinkFactory(service=azure_service, cloud_service_name=spl_cloud_name)

        self.assertEquals(spl.cloud_service_name, spl_cloud_name)
