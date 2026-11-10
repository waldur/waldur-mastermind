"""Agent and staff write-back actions must not undo a concurrent change.

Each test lets a "concurrent" request commit while the action under test holds
its copy of the object, which is how two overlapping requests interleave.
Saving every field of that stale copy would write the old values back: a
resource restored to OK went back to Creating that way.
"""

from contextlib import contextmanager
from datetime import timedelta
from unittest import mock

from ddt import data, ddt
from django.conf import settings
from django.test import override_settings
from django.utils import timezone
from rest_framework import status, test

from waldur_mastermind.marketplace import models, views
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories, fixtures


@contextmanager
def concurrent_write_before_save(model, **changes):
    """Commit ``changes`` to the row right before the view saves its copy.

    Works however often the view loads the object: an action with validators
    loads it once for them and again in its body, so a write after the first
    load would only be read back, never overwritten.
    """
    original_save = model.save
    fired = []

    def save(self, *args, **kwargs):
        if not fired:
            fired.append(True)
            model.objects.filter(pk=self.pk).update(**changes)
        return original_save(self, *args, **kwargs)

    with mock.patch.object(model, "save", save):
        yield


@contextmanager
def concurrent_write_after_load(viewset, model, **changes):
    """Commit ``changes`` to the row right after the view has loaded its copy.

    For actions without validators, which load the object only once, and for
    decisions taken on the loaded copy before anything is saved.
    """
    original_get_object = viewset.get_object

    def get_object(view):
        obj = original_get_object(view)
        model.objects.filter(pk=obj.pk).update(**changes)
        return obj

    with mock.patch.object(viewset, "get_object", get_object):
        yield


class ProviderResourceWriteBackTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.client.force_authenticate(self.fixture.staff)

    def post(self, action, payload=None):
        url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, action=action
        )
        return self.client.post(url, payload or {})

    def test_set_backend_metadata_keeps_concurrent_state(self):
        self.assertEqual(self.resource.state, ResourceStates.CREATING)

        with concurrent_write_before_save(models.Resource, state=ResourceStates.OK):
            response = self.post(
                "set_backend_metadata", {"backend_metadata": {"qos": "normal"}}
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.OK)
        self.assertEqual(self.resource.backend_metadata, {"qos": "normal"})

    def test_set_backend_id_keeps_concurrent_state(self):
        with concurrent_write_before_save(models.Resource, state=ResourceStates.OK):
            response = self.post("set_backend_id", {"backend_id": "new-backend-id"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.OK)
        self.assertEqual(self.resource.backend_id, "new-backend-id")

    def test_set_as_erred_keeps_concurrent_change(self):
        with concurrent_write_before_save(
            models.Resource, backend_metadata={"qos": "normal"}
        ):
            response = self.post("set_as_erred", {"error_message": "Backend failed."})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.ERRED)
        self.assertEqual(self.resource.error_message, "Backend failed.")
        self.assertEqual(self.resource.backend_metadata, {"qos": "normal"})

    def test_set_as_ok_keeps_concurrent_change(self):
        models.Resource.objects.filter(pk=self.resource.pk).update(
            state=ResourceStates.ERRED, error_message="Backend failed."
        )

        with concurrent_write_before_save(
            models.Resource, backend_metadata={"qos": "normal"}
        ):
            response = self.post("set_as_ok")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.OK)
        self.assertEqual(self.resource.error_message, "")
        self.assertEqual(self.resource.backend_metadata, {"qos": "normal"})

    def test_provider_set_end_date_keeps_concurrent_state(self):
        end_date = timezone.now().date() + timedelta(days=30)

        with concurrent_write_before_save(models.Resource, state=ResourceStates.OK):
            response = self.post("set_end_date", {"end_date": end_date.isoformat()})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.OK)
        self.assertEqual(self.resource.end_date, end_date)
        self.assertEqual(self.resource.end_date_requested_by, self.fixture.staff)

    def test_set_end_date_by_provider_keeps_concurrent_state(self):
        end_date = timezone.now().date() + timedelta(days=30)

        with concurrent_write_before_save(models.Resource, state=ResourceStates.OK):
            response = self.post(
                "set_end_date_by_provider", {"end_date": end_date.isoformat()}
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.OK)
        self.assertEqual(self.resource.end_date, end_date)
        self.assertEqual(self.resource.end_date_requested_by, self.fixture.staff)

    def test_set_as_ok_decides_on_the_current_state(self):
        # The copy the view loaded is still Erred, but the resource has been
        # terminated since: it must not come back to life.
        models.Resource.objects.filter(pk=self.resource.pk).update(
            state=ResourceStates.ERRED
        )

        with concurrent_write_after_load(
            views.ProviderResourceViewSet,
            models.Resource,
            state=ResourceStates.TERMINATED,
        ):
            response = self.post("set_as_ok")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.TERMINATED)


@ddt
class ResourceFlagWriteBackTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.client.force_authenticate(self.fixture.staff)

    @data(
        ("set_downscaled", "downscaled"),
        ("set_paused", "paused"),
        ("set_restrict_member_access", "restrict_member_access"),
    )
    def test_flag_change_keeps_concurrent_state(self, action_and_field):
        action, field = action_and_field
        url = factories.ResourceFactory.get_url(self.resource, action)

        with concurrent_write_after_load(
            views.ConsumerResourceViewSet, models.Resource, state=ResourceStates.OK
        ):
            response = self.client.post(url, {field: True})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.OK)
        self.assertTrue(getattr(self.resource, field))

    def test_consumer_set_end_date_keeps_concurrent_state(self):
        end_date = timezone.now().date() + timedelta(days=30)
        url = factories.ResourceFactory.get_url(self.resource, "set_end_date")

        with concurrent_write_before_save(models.Resource, state=ResourceStates.OK):
            response = self.client.post(url, {"end_date": end_date.isoformat()})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.OK)
        self.assertEqual(self.resource.end_date, end_date)


class OrderWriteBackTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = SITE_AGENT_OFFERING
        self.offering.save()
        self.resource = self.fixture.resource
        self.client.force_authenticate(self.fixture.staff)

    def make_order(self, order_type):
        return factories.OrderFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.fixture.plan,
            resource=self.resource,
            type=order_type,
            state=OrderStates.EXECUTING,
        )

    def test_order_set_backend_id_keeps_concurrent_state(self):
        order = self.make_order(OrderTypes.CREATE)
        url = factories.OrderFactory.get_url(order, "set_backend_id")

        with concurrent_write_before_save(models.Order, state=OrderStates.DONE):
            response = self.client.post(url, {"backend_id": "new-order-backend-id"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.DONE)
        self.assertEqual(order.backend_id, "new-order-backend-id")

    def test_set_state_done_on_restore_leaves_resource_ok(self):
        self.assertEqual(self.resource.state, ResourceStates.CREATING)
        order = self.make_order(OrderTypes.RESTORE)
        url = factories.OrderFactory.get_url(order, "set_state_done")

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.resource.refresh_from_db()
        self.assertEqual(order.state, OrderStates.DONE)
        self.assertEqual(self.resource.state, ResourceStates.OK)

    @override_settings(
        WALDUR_CORE={**settings.WALDUR_CORE, "USE_ATOMIC_TRANSACTION": False}
    )
    def test_set_state_done_rolls_back_order_if_resource_step_fails(self):
        # The order is completed first and the resource moved after it: if the
        # second step fails, the order must not stay DONE on its own. The
        # request transaction is turned off, as an operator may do, so that
        # only the action's own transaction can roll the order back.
        order = self.make_order(OrderTypes.RESTORE)
        url = factories.OrderFactory.get_url(order, "set_state_done")

        with mock.patch.object(
            models.Resource, "save", side_effect=RuntimeError("resource step failed")
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(url)

        order.refresh_from_db()
        self.resource.refresh_from_db()
        self.assertEqual(order.state, OrderStates.EXECUTING)
        self.assertEqual(self.resource.state, ResourceStates.CREATING)
