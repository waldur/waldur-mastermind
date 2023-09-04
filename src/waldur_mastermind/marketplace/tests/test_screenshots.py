from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.media.utils import dummy_image
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.utils import add_permission
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests.helpers import override_marketplace_settings

from . import factories


@ddt
class ScreenshotsGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.screenshot = factories.ScreenshotFactory()

    @data('staff', 'owner', 'user', 'customer_support', 'admin', 'manager')
    def test_screenshots_should_be_visible_to_all_authenticated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ScreenshotFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    @override_marketplace_settings(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_screenshots_should_be_invisible_to_unauthenticated_users(self):
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @data('staff', 'owner', 'user', 'customer_support', 'admin', 'manager')
    def test_screenshots_of_offering_should_be_visible_to_all_authenticated_users(
        self, user
    ):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        offering = self.screenshot.offering
        url = factories.ScreenshotFactory.get_list_url()
        response = self.client.get(url, {'offering_uuid': offering.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)


@ddt
class ScreenshotsCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        add_permission(
            RoleEnum.CUSTOMER_OWNER, PermissionEnum.CREATE_OFFERING_SCREENSHOT
        )

    @data('staff', 'owner')
    def test_authorized_user_can_create_screenshot(self, user):
        response = self.create_screenshot(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Screenshot.objects.filter(offering__customer=self.customer).exists()
        )

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_create_screenshot(self, user):
        response = self.create_screenshot(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch('waldur_mastermind.marketplace.handlers.tasks')
    def test_create_thumbnail(self, mock_tasks):
        response = self.create_screenshot('staff')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(mock_tasks.create_screenshot_thumbnail.delay.call_count, 1)

    def create_screenshot(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ScreenshotFactory.get_list_url()
        self.provider = factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer)

        payload = {
            'name': 'screenshot',
            'offering': factories.OfferingFactory.get_url(offering=self.offering),
            'image': dummy_image(),
        }

        return self.client.post(url, payload, format='multipart')


@ddt
class ScreenshotsUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        add_permission(
            RoleEnum.CUSTOMER_OWNER, PermissionEnum.UPDATE_OFFERING_SCREENSHOT
        )

    @data('staff', 'owner')
    def test_authorized_user_can_update_screenshot(self, user):
        response, screenshot = self.update_screenshot(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(screenshot.name, 'new_screenshot')
        self.assertTrue(
            models.Screenshot.objects.filter(name='new_screenshot').exists()
        )

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_update_screenshot(self, user):
        response, offering = self.update_screenshot(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def update_screenshot(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        self.provider = factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer)
        screenshot = factories.ScreenshotFactory(offering=self.offering)
        url = factories.ScreenshotFactory.get_url(screenshot=screenshot)

        response = self.client.patch(url, {'name': 'new_screenshot'})
        screenshot.refresh_from_db()

        return response, screenshot


@ddt
class ScreenshotsDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.provider = factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer)
        self.screenshot = factories.ScreenshotFactory(offering=self.offering)
        add_permission(
            RoleEnum.CUSTOMER_OWNER, PermissionEnum.DELETE_OFFERING_SCREENSHOT
        )

    @data('staff', 'owner')
    def test_authorized_user_can_delete_screenshot(self, user):
        response = self.delete_screenshot(user)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(
            models.Screenshot.objects.filter(offering__customer=self.customer).exists()
        )

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_delete_screenshot(self, user):
        response = self.delete_screenshot(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            models.Screenshot.objects.filter(offering__customer=self.customer).exists()
        )

    def delete_screenshot(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ScreenshotFactory.get_url(self.screenshot)
        response = self.client.delete(url)
        return response
