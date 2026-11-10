"""Whether a person at the provider acts on every order on an offering: the
provider reviews it, or a helpdesk ticket fulfils it. An automatic allocation
decision relying on the provider fulfilment reads this per offering."""

from rest_framework import test

from waldur_core.core.utils import get_system_robot
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import utils
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    REMOTE_OFFERING,
    SCRIPT_OFFERING,
    SITE_AGENT_OFFERING,
    SUPPORT_OFFERING,
)
from waldur_mastermind.marketplace.tests import factories


class OfferingOrdersNeedProviderActionTest(test.APITestCase):
    def setUp(self):
        self.robot = get_system_robot()
        self.user = structure_factories.UserFactory()

    def _needs(self, offering_type, user=None, **plugin_options):
        offering = factories.OfferingFactory(
            type=offering_type, plugin_options=plugin_options
        )
        return utils.offering_orders_need_provider_action(offering, user or self.robot)

    def test_provider_reviews_basic_and_site_agent_orders(self):
        self.assertTrue(self._needs(BASIC_OFFERING))
        self.assertTrue(self._needs(SITE_AGENT_OFFERING))

    def test_a_helpdesk_ticket_fulfils_support_orders(self):
        self.assertTrue(self._needs(SUPPORT_OFFERING))

    def test_script_orders_need_review_only_with_auto_approval_off(self):
        self.assertTrue(
            self._needs(SCRIPT_OFFERING, auto_approve_marketplace_script=False)
        )
        self.assertFalse(
            self._needs(SCRIPT_OFFERING, auto_approve_marketplace_script=True)
        )
        self.assertFalse(self._needs(SCRIPT_OFFERING))

    def test_remote_orders_approved_automatically_need_nobody(self):
        self.assertFalse(
            self._needs(REMOTE_OFFERING, self.user, auto_approve_remote_orders=True)
        )

    def test_remote_orders_need_review_from_an_ordinary_consumer(self):
        self.assertTrue(self._needs(REMOTE_OFFERING, self.user))

    def test_remote_orders_approved_by_the_system_robot_need_nobody(self):
        # The robot is staff, which the provider review lets through as an
        # owner: an allocation's orders on a remote offering go on unreviewed.
        self.assertFalse(self._needs(REMOTE_OFFERING, self.robot))

    def test_other_offerings_are_provisioned_without_anyone(self):
        self.assertFalse(self._needs("Marketplace.Unreviewed"))

    def test_the_router_reads_the_same_rules(self):
        for offering_type, options in (
            (BASIC_OFFERING, {}),
            (SUPPORT_OFFERING, {}),
            (REMOTE_OFFERING, {}),
            (SCRIPT_OFFERING, {"auto_approve_marketplace_script": False}),
            ("Marketplace.Unreviewed", {}),
        ):
            offering = factories.OfferingFactory(
                type=offering_type, plugin_options=options
            )
            order = factories.OrderFactory(
                offering=offering, consumer_reviewed_by=self.user
            )
            reviewed = not utils.order_should_not_be_reviewed_by_provider(order)
            needs = utils.offering_orders_need_provider_action(offering, self.user)
            if offering_type == SUPPORT_OFFERING:
                # Not reviewed: its ticket decides the order.
                self.assertFalse(reviewed)
                self.assertTrue(needs)
            else:
                self.assertEqual(reviewed, needs, offering_type)
