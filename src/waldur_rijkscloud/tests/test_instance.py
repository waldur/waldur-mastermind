from rest_framework import status, test

from . import factories, fixtures


class InstanceCreateTest(test.APITransactionTestCase):
    def setUp(self):
        super(InstanceCreateTest, self).setUp()
        self.fixture = fixtures.RijkscloudFixture()

    def create_instance(self, spl, flavor, internal_ip, floating_ip):
        url = factories.InstanceFactory.get_list_url()
        return self.client.post(url, {
            'name': 'Test Instance',
            'service_project_link': factories.ServiceProjectLinkFactory.get_url(spl),
            'flavor': factories.FlavorFactory.get_url(flavor),
            'internal_ip': factories.InternalIPFactory.get_url(internal_ip),
            'floating_ip': factories.FloatingIPFactory.get_url(floating_ip),
        })

    def test_user_can_create_instance(self):
        self.client.force_login(self.fixture.owner)
        response = self.create_instance(
            self.fixture.spl, self.fixture.flavor, self.fixture.internal_ip, self.fixture.floating_ip)
        self.assertEqual(status.HTTP_201_CREATED, response.status_code)

    def test_user_can_not_create_instance_if_floating_ip_is_not_available(self):
        # Arrange
        self.fixture.floating_ip.is_available = False
        self.fixture.floating_ip.save()

        # Act
        self.client.force_login(self.fixture.owner)
        response = self.create_instance(
            self.fixture.spl, self.fixture.flavor, self.fixture.internal_ip, self.fixture.floating_ip)

        # Assert
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertTrue('floating_ip' in response.data)

    def test_user_can_not_create_instance_if_internal_ip_is_not_available(self):
        # Arrange
        self.fixture.internal_ip.is_available = False
        self.fixture.internal_ip.save()

        # Act
        self.client.force_login(self.fixture.owner)
        response = self.create_instance(
            self.fixture.spl, self.fixture.flavor, self.fixture.internal_ip, self.fixture.floating_ip)

        # Assert
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertTrue('internal_ip' in response.data)

    def test_after_instance_is_created_floating_ip_is_marked_as_not_available(self):
        self.client.force_login(self.fixture.owner)
        response = self.create_instance(
            self.fixture.spl, self.fixture.flavor, self.fixture.internal_ip, self.fixture.floating_ip)

        self.assertEqual(status.HTTP_201_CREATED, response.status_code)
        self.fixture.floating_ip.refresh_from_db()
        self.assertFalse(self.fixture.floating_ip.is_available)

    def test_after_instance_is_created_internal_ip_is_marked_as_not_available(self):
        self.client.force_login(self.fixture.owner)
        response = self.create_instance(
            self.fixture.spl, self.fixture.flavor, self.fixture.internal_ip, self.fixture.floating_ip)

        self.assertEqual(status.HTTP_201_CREATED, response.status_code)
        self.fixture.internal_ip.refresh_from_db()
        self.assertFalse(self.fixture.internal_ip.is_available)
