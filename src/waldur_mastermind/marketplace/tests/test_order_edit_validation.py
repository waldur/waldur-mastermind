"""Editing a pending create order runs its offering's check again.

The check runs at order creation with the request, so a personal access
token's bindings and the requester's rights apply to what the order asks for.
Execution later runs with the user's own API token, without those limits, so
an edit must be checked as creation was.
"""

import copy
from unittest import mock

from django.utils import timezone
from rest_framework import exceptions, status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.enums import (
    OfferingStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.models import Order
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.marketplace.tests.test_derived_limits import (
    DERIVED_AT_200,
    PAIRED_RESOURCE_OPTIONS,
    DerivedLimitsOrderMixin,
)

CHECK = "waldur_mastermind.marketplace.utils.validate_order"


class OrderEditTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE, customer=self.fixture.customer
        )
        self.order = factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.admin,
            offering=offering,
            state=OrderStates.PENDING_CONSUMER,
            type=OrderTypes.CREATE,
            attributes={"name": "before"},
        )
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.fixture.admin)

    def edit(self, payload):
        return self.client.patch(
            factories.OrderFactory.get_url(self.order), payload, format="json"
        )

    def test_the_check_sees_the_edited_order_and_the_editing_request(self):
        with mock.patch(CHECK) as check:
            response = self.edit({"attributes": {"name": "after"}})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        check.assert_called_once()
        order, request = check.call_args[0]
        self.assertEqual(order.attributes, {"name": "after"})
        self.assertEqual(request.user, self.fixture.admin)

    def test_an_edit_the_check_refuses_is_refused_and_changes_nothing(self):
        with mock.patch(CHECK, side_effect=exceptions.ValidationError("refused")):
            response = self.edit({"attributes": {"name": "after"}})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.order.refresh_from_db()
        self.assertEqual(self.order.attributes, {"name": "before"})

    def test_an_edit_of_an_order_approved_meanwhile_is_refused(self):
        def approve_meanwhile(order, request):
            Order.objects.filter(pk=self.order.pk).update(state=OrderStates.EXECUTING)

        with mock.patch(CHECK, side_effect=approve_meanwhile):
            response = self.edit({"attributes": {"name": "after"}})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.EXECUTING)
        self.assertEqual(self.order.attributes, {"name": "before"})

    def test_an_edit_of_an_order_edited_meanwhile_is_refused(self):
        def edit_meanwhile(order, request):
            Order.objects.filter(pk=self.order.pk).update(
                attributes={"name": "concurrent"}, modified=timezone.now()
            )

        with mock.patch(CHECK, side_effect=edit_meanwhile):
            response = self.edit({"attributes": {"name": "after"}})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.order.refresh_from_db()
        self.assertEqual(self.order.attributes, {"name": "concurrent"})

    def test_an_edit_of_an_update_order_is_not_checked_as_a_creation(self):
        self.order.type = OrderTypes.UPDATE
        self.order.save()

        with mock.patch(CHECK) as check:
            self.edit({"attributes": {"name": "after"}})

        check.assert_not_called()


class OrderLimitsEditTest(DerivedLimitsOrderMixin, test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        offering, plan = self.build_offering()
        offering.customer = self.fixture.customer
        offering.save()
        self.order = factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.admin,
            offering=offering,
            plan=plan,
            state=OrderStates.PENDING_CONSUMER,
            type=OrderTypes.CREATE,
            attributes={"storage": 200},
        )
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.fixture.admin)

    def test_an_edit_of_the_limits_is_checked_too(self):
        with mock.patch(CHECK) as check:
            response = self.client.patch(
                factories.OrderFactory.get_url(self.order),
                {"limits": {"extra": 3}},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        check.assert_called_once()


RESOURCE_OPTIONS = {
    "order": ["namespace", "label"],
    "options": {
        "namespace": {"type": "string", "label": "Namespace", "unique": True},
        "label": {"type": "string", "label": "Label"},
    },
}


class OptionChangeEditTest(test.APITestCase):
    """A pending option-change order is edited only as update_options would
    make the change, and stays a change from the options it was placed over."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        offering = self.fixture.offering
        offering.resource_options = copy.deepcopy(RESOURCE_OPTIONS)
        offering.save()
        self.resource = self.fixture.resource
        self.resource.options = {"namespace": "team-a"}
        self.resource.save()
        factories.ResourceFactory(
            offering=offering, state=ResourceStates.OK, options={"namespace": "team-b"}
        )
        self.order = factories.OrderFactory(
            resource=self.resource,
            offering=offering,
            project=self.resource.project,
            type=OrderTypes.UPDATE,
            state=OrderStates.PENDING_CONSUMER,
            attributes={
                "old_options": {"namespace": "team-a"},
                "new_options": {"namespace": "team-c"},
            },
        )
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.fixture.admin)

    def edit(self, attributes):
        return self.client.patch(
            factories.OrderFactory.get_url(self.order),
            {"attributes": attributes},
            format="json",
        )

    def stored(self):
        self.order.refresh_from_db()
        return self.order.attributes

    def test_a_value_another_resource_holds_is_refused(self):
        response = self.edit({"new_options": {"namespace": "team-b"}})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.stored()["new_options"], {"namespace": "team-c"})

    def test_a_free_value_is_stored(self):
        response = self.edit({"new_options": {"namespace": "team-d"}})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.stored()["new_options"]["namespace"], "team-d")

    def test_the_options_it_was_placed_over_stay(self):
        response = self.edit(
            {"old_options": {"namespace": "x"}, "new_options": {"namespace": "team-d"}}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.stored()["old_options"], {"namespace": "team-a"})

    def test_leaving_out_the_change_keeps_it(self):
        response = self.edit({})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.stored()["new_options"], {"namespace": "team-c"})


class OptionChangeKindEditTest(DerivedLimitsOrderMixin, test.APITestCase):
    """An edit keeps an option change the kind update_options made it: a
    change of a formula input, with the limits derived from it, or a change of
    other options, without limits."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        offering, plan = self.build_offering()
        offering.customer = self.fixture.customer
        resource_options = copy.deepcopy(PAIRED_RESOURCE_OPTIONS)
        resource_options["order"].append("label")
        resource_options["options"]["label"] = {"type": "string", "label": "Label"}
        offering.resource_options = resource_options
        offering.save()
        self.resource = factories.ResourceFactory(
            offering=offering,
            plan=plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
            attributes={"storage": 200},
            options={"storage": 200, "label": "a"},
            limits=dict(DERIVED_AT_200),
        )
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.fixture.admin)

    def place(self, attributes, limits=None):
        order = factories.OrderFactory(
            resource=self.resource,
            offering=self.resource.offering,
            plan=self.resource.plan,
            project=self.resource.project,
            type=OrderTypes.UPDATE,
            state=OrderStates.PENDING_CONSUMER,
            attributes=attributes,
            limits=limits or {},
        )
        Order.objects.filter(pk=order.pk).update(state=OrderStates.PENDING_CONSUMER)
        return order

    def edit(self, order, new_options):
        return self.client.patch(
            factories.OrderFactory.get_url(order),
            {"attributes": {"new_options": new_options}},
            format="json",
        )

    def test_a_label_change_cannot_be_made_to_change_the_storage(self):
        order = self.place(
            {
                "old_options": {"storage": 200, "label": "a"},
                "new_options": {"storage": 200, "label": "b"},
            }
        )

        response = self.edit(order, {"storage": 5000, "label": "b"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        order.refresh_from_db()
        self.assertEqual(order.attributes["new_options"]["storage"], 200)
        self.assertEqual(order.limits, {})

    def test_a_storage_change_cannot_be_made_to_leave_the_storage(self):
        order = self.place(
            {
                "old_options": {"storage": 200, "label": "a"},
                "new_options": {"storage": 100, "label": "a"},
                "old_limits": dict(DERIVED_AT_200),
            },
            limits={**DERIVED_AT_200, "data_primary": 200},
        )

        response = self.edit(order, {"storage": 200, "label": "b"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        order.refresh_from_db()
        self.assertEqual(order.attributes["new_options"]["storage"], 100)

    def test_a_label_change_can_be_edited_to_another_label(self):
        order = self.place(
            {
                "old_options": {"storage": 200, "label": "a"},
                "new_options": {"storage": 200, "label": "b"},
            }
        )

        response = self.edit(order, {"storage": 200, "label": "c"})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order.refresh_from_db()
        self.assertEqual(order.attributes["new_options"]["label"], "c")

    def test_a_plan_switch_cannot_be_made_an_option_change(self):
        order = self.place({})

        response = self.edit(order, {"storage": 200, "label": "b"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        order.refresh_from_db()
        self.assertNotIn("new_options", order.attributes)

    def test_a_plan_switch_does_not_take_empty_new_options(self):
        # The key alone would make it execute as an option change.
        order = self.place({})

        response = self.edit(order, None)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order.refresh_from_db()
        self.assertNotIn("new_options", order.attributes)

    def test_new_options_must_be_an_object(self):
        order = self.place(
            {
                "old_options": {"storage": 200, "label": "a"},
                "new_options": {"storage": 200, "label": "b"},
            }
        )

        response = self.edit(order, "storage")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def edit_limits(self, order, limits):
        return self.client.patch(
            factories.OrderFactory.get_url(order), {"limits": limits}, format="json"
        )

    def test_the_limits_of_an_option_change_cannot_be_edited(self):
        order = self.place(
            {
                "old_options": {"storage": 200, "label": "a"},
                "new_options": {"storage": 200, "label": "b"},
            }
        )

        response = self.edit_limits(order, {"extra": 3})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        order.refresh_from_db()
        self.assertEqual(order.limits, {})

    def test_the_limits_of_a_plan_switch_cannot_be_edited(self):
        order = self.place({}, limits={"extra": 1})

        response = self.edit_limits(order, {"extra": 3})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        order.refresh_from_db()
        self.assertEqual(order.limits, {"extra": 1})

    def test_the_limits_of_a_limit_change_can_still_be_edited(self):
        order = self.place(
            {"old_limits": {"extra": 1}}, limits={**DERIVED_AT_200, "extra": 2}
        )

        response = self.edit_limits(order, {**DERIVED_AT_200, "extra": 3})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order.refresh_from_db()
        self.assertEqual(order.limits["extra"], 3)
