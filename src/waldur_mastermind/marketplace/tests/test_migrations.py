from django.apps import apps
from django.utils import timezone
from rest_framework import test

from waldur_mastermind.marketplace import models, utils
from waldur_mastermind.marketplace.tests import factories


class FillActivatedFieldTest(test.APITransactionTestCase):
    def setUp(self):
        self.resource = factories.ResourceFactory()
        self.order_item = factories.OrderItemFactory(resource=self.resource)
        self.order_item.state = models.OrderItem.States.DONE
        self.order_item.save()

    def test_activated_field_has_been_succeeded_filled(self):
        self.assertFalse(self.order_item.activated)
        utils.fill_activated_field(apps, None)
        self.order_item.refresh_from_db()
        self.assertEqual(self.order_item.activated, self.resource.created)

    def test_activated_field_does_not_override_if_it_is_filled(self):
        now = timezone.now()
        self.order_item.activated = now
        self.order_item.save()
        utils.fill_activated_field(apps, None)
        self.order_item.refresh_from_db()
        self.assertNotEqual(self.order_item.activated, self.resource.created)
        self.assertEqual(self.order_item.activated, now)

    def test_activated_field_is_not_filled_if_state_is_not_done(self):
        self.order_item.state = models.OrderItem.States.EXECUTING
        self.order_item.save()
        self.assertFalse(self.order_item.activated)
        utils.fill_activated_field(apps, None)
        self.order_item.refresh_from_db()
        self.assertFalse(self.order_item.activated)

    def test_activated_field_is_not_filled_if_type_is_not_create(self):
        self.order_item.type = models.RequestTypeMixin.Types.UPDATE
        self.order_item.save()
        self.assertFalse(self.order_item.activated)
        utils.fill_activated_field(apps, None)
        self.order_item.refresh_from_db()
        self.assertFalse(self.order_item.activated)
