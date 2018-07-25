from ddt import ddt, data
from rest_framework import test, status

from waldur_core.structure import models
from . import fixtures, factories


class BaseServiceCertificationTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()


@ddt
class ServiceCertificationRetrieveTest(BaseServiceCertificationTest):

    def setUp(self):
        super(ServiceCertificationRetrieveTest, self).setUp()
        self.url = factories.ServiceCertificationFactory.get_list_url()

    @data('user', 'global_support', 'owner', 'manager', 'admin', 'staff')
    def test_user_can_see_list_of_available_certifications(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        certification = factories.ServiceCertificationFactory()

        response = self.client.get(self.url)

        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['uuid'], certification.uuid.hex)


@ddt
class ServiceCertificationCreateTest(BaseServiceCertificationTest):

    def setUp(self):
        super(ServiceCertificationCreateTest, self).setUp()
        self.url = factories.ServiceCertificationFactory.get_list_url()

    @data('staff')
    def test_staff_can_create_certification(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {
            'name': 'ISO3728',
            'link': 'http://www.iso.org/unknown',
            'description': 'this is a description',
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        certification = models.ServiceCertification.objects.get(name=payload['name'])
        self.assertEqual(response.data['name'], payload['name'])
        self.assertEqual(certification.name, payload['name'])
        self.assertEqual(response.data['link'], payload['link'])
        self.assertEqual(certification.link, payload['link'])
        self.assertEqual(response.data['description'], payload['description'])
        self.assertEqual(certification.description, payload['description'])

    @data('user', 'global_support', 'owner', 'manager', 'admin')
    def test_only_staff_can_create_certification(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {
            'name': 'ISO3728',
            'link': 'http://www.iso.org/unknown',
            'description': 'this is a description',
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ServiceCertificationUpdateTest(BaseServiceCertificationTest):

    def setUp(self):
        super(ServiceCertificationUpdateTest, self).setUp()
        self.certification = factories.ServiceCertificationFactory()
        self.url = factories.ServiceCertificationFactory.get_url(self.certification)

    @data('staff')
    def test_staff_can_update_certification(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        expected_name = 'new_name'

        response = self.client.put(self.url, {'name': expected_name})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.certification.refresh_from_db()
        self.assertEqual(self.certification.name, expected_name)

    @data('admin', 'manager', 'global_support', 'user')
    def test_only_staff_can_update_certification(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.put(self.url, {'name': 'new_name'})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ServiceCertificationDeleteTest(BaseServiceCertificationTest):

    def setUp(self):
        super(ServiceCertificationDeleteTest, self).setUp()
        self.certification = factories.ServiceCertificationFactory()
        self.url = factories.ServiceCertificationFactory.get_url(self.certification)

    @data('staff')
    def test_staff_can_delete_certification(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        with self.assertRaises(models.ServiceCertification.DoesNotExist):
            self.certification.refresh_from_db()

    @data('admin', 'manager', 'global_support', 'user')
    def test_only_staff_can_delete_certification(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.ServiceCertification.objects.filter(pk=self.certification.pk).exists())
