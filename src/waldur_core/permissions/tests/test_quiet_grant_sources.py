from django.contrib.contenttypes.models import ContentType
from django.core import mail
from rest_framework import test

from waldur_core.logging import models as logging_models
from waldur_core.logging.tasks import process_event
from waldur_core.permissions import utils
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.models import Role
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories


class QuietGrantSourceTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.owner = structure_factories.UserFactory()
        utils.add_user(self.customer, self.owner, CustomerRole.OWNER)
        self.user = structure_factories.UserFactory()
        logging_models.EmailHook.objects.create(
            user=self.owner,
            email=self.owner.email,
            event_types=["role_granted", "role_revoked"],
        )
        self.addCleanup(utils.QUIET_GRANT_SOURCE_PREFIXES.discard, "test-sync:")
        utils.register_quiet_grant_source("test-sync:")
        mail.outbox = []

    def events(self):
        return logging_models.Event.objects.filter(
            event_type__in=["role_granted", "role_revoked"],
            context__affected_user_uuid=self.user.uuid.hex,
        ).order_by("created")

    def deliver(self):
        for event in self.events():
            process_event(event.id)

    def test_quiet_source_is_logged_but_not_emailed(self):
        user_role = utils.add_user(
            self.customer, self.user, CustomerRole.SUPPORT, source="test-sync:1"
        )
        user_role.revoke(reason="left")

        events = list(self.events())
        self.assertEqual(len(events), 2)
        for event in events:
            self.assertEqual(event.context["role_source"], "test-sync:1")
            self.assertTrue(event.context["suppress_email"])
        self.deliver()
        self.assertEqual(mail.outbox, [])

    def test_other_sources_are_emailed(self):
        utils.add_user(
            self.customer, self.user, CustomerRole.SUPPORT, source="rule:abc"
        )
        event = self.events().get()
        self.assertEqual(event.context["role_source"], "rule:abc")
        self.assertNotIn("suppress_email", event.context)
        self.deliver()
        self.assertEqual(len(mail.outbox), 1)

    def test_manual_grants_are_emailed(self):
        utils.add_user(self.customer, self.user, CustomerRole.SUPPORT)
        self.assertNotIn("role_source", self.events().get().context)
        self.deliver()
        self.assertEqual(len(mail.outbox), 1)

    def test_is_quiet_grant_source(self):
        self.assertTrue(utils.is_quiet_grant_source("test-sync:x"))
        self.assertFalse(utils.is_quiet_grant_source("test-other:x"))
        self.assertFalse(utils.is_quiet_grant_source(""))


class CustomRoleUserQuotaTest(test.APITestCase):
    def test_custom_organization_role_counts_toward_user_quota(self):
        customer = structure_factories.CustomerFactory()
        role = Role.objects.create(
            name="CUSTOMER.custom.MEMBER",
            content_type=ContentType.objects.get_for_model(Customer),
        )
        user = structure_factories.UserFactory()

        user_role = utils.add_user(customer, user, role)
        self.assertEqual(customer.get_quota_usage("nc_user_count"), 1)

        user_role.revoke()
        self.assertEqual(customer.get_quota_usage("nc_user_count"), 0)
