from ddt import ddt, data
from rest_framework import status

from waldur_core.structure.tests import factories as structure_factories

from . import factories
from .base_test import BaseCostTrackingTest
from .. import models


@ddt
class PriceListItemListTest(BaseCostTrackingTest):

    def setUp(self):
        super(PriceListItemListTest, self).setUp()
        self.price_list_item = factories.PriceListItemFactory(service=self.service)

    @data('staff', 'owner', 'manager')
    def test_user_with_access_to_service_can_see_services_price_list(self, user):
        self.client.force_authenticate(self.users[user])
        response = self.client.get(factories.PriceListItemFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(self.price_list_item.uuid.hex, [el['uuid'] for el in response.data])

    @data('administrator')
    def test_user_without_access_to_service_cannot_see_services_price_list(self, user):
        self.service_project_link.delete()
        self.client.force_authenticate(self.users[user])
        response = self.client.get(factories.PriceListItemFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn(self.price_list_item.uuid.hex, [el['uuid'] for el in response.data])

    def test_price_list_can_be_filtered_by_service(self):
        other_price_list_item = factories.PriceListItemFactory()

        self.client.force_authenticate(self.users['staff'])
        response = self.client.get(
            factories.PriceListItemFactory.get_list_url(),
            data={'service': structure_factories.TestServiceFactory.get_url(self.service)}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(self.price_list_item.uuid.hex, [el['uuid'] for el in response.data])
        self.assertNotIn(other_price_list_item.uuid.hex, [el['uuid'] for el in response.data])


@ddt
class PriceListItemCreateTest(BaseCostTrackingTest):

    def setUp(self):
        super(PriceListItemCreateTest, self).setUp()
        self.default_price_list_item = factories.DefaultPriceListItemFactory()
        self.valid_data = {
            'service': structure_factories.TestServiceFactory.get_url(self.service),
            'default_price_list_item': factories.DefaultPriceListItemFactory.get_url(self.default_price_list_item),
            'value': 100,
            'units': 'UAH'
        }

    @data('staff', 'owner')
    def test_user_with_permissions_can_create_price_list_item(self, user):
        self.client.force_authenticate(self.users[user])
        response = self.client.post(factories.PriceListItemFactory.get_list_url(), data=self.valid_data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.PriceListItem.objects.filter(
            service=self.service,
            value=self.valid_data['value'],
            default_price_list_item=self.default_price_list_item).exists())

    @data('manager', 'administrator')
    def test_user_without_permissions_cannot_create_price_list_item(self, user):
        self.client.force_authenticate(self.users[user])
        response = self.client.post(factories.PriceListItemFactory.get_list_url(), data=self.valid_data)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, str(response.data) + " " + user)
        self.assertFalse(models.PriceListItem.objects.filter(
            service=self.service,
            value=self.valid_data['value'],
            default_price_list_item=self.default_price_list_item).exists())

    def test_if_price_list_item_already_exists_validation_error_is_raised(self):
        self.client.force_authenticate(self.users['staff'])
        response = self.client.post(factories.PriceListItemFactory.get_list_url(), data=self.valid_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self.client.post(factories.PriceListItemFactory.get_list_url(), data=self.valid_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class PriceListItemUpdateTest(BaseCostTrackingTest):

    def setUp(self):
        super(PriceListItemUpdateTest, self).setUp()
        self.price_list_item = factories.PriceListItemFactory(service=self.service)

    @data('staff', 'owner')
    def test_user_with_permissions_can_update_price_list_item(self, user):
        self.client.force_authenticate(self.users[user])
        data = {'value': 200}
        response = self.client.patch(factories.PriceListItemFactory.get_url(self.price_list_item), data=data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        reread_price_list_item = models.PriceListItem.objects.get(id=self.price_list_item.id)
        self.assertEqual(reread_price_list_item.value, data['value'])

    # We do not execute this test for administrator, because he does not see price estimates at all
    @data('manager')
    def test_user_without_permissions_cannot_update_price_list_item(self, user):
        self.client.force_authenticate(self.users[user])
        data = {'items': [{'name': 'cpu', 'value': 1000, 'units': 'USD per CPU'}]}
        response = self.client.patch(factories.PriceListItemFactory.get_url(self.price_list_item), data=data)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
