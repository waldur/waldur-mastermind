"""Access subnets as one list per organization, scoped to portal and/or offerings.

An organization keeps a single list of trusted networks. Each entry says what it
is trusted for: signing in to the portal, reaching resources of particular
offerings, or both. These tests cover the scope rules, the mask and provenance
rules that apply to every entry, and the two enforcement paths that read them.
"""

from ddt import data, ddt
from django.core.management import call_command
from django.urls import reverse
from rest_framework import test

from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories, fixtures


def _list_url():
    return reverse("access-subnets-list")


def _detail_url(subnet):
    return reverse("access-subnets-detail", kwargs={"uuid": subnet.uuid.hex})


def _enable_subnets(offering):
    offering.plugin_options = {"enable_resource_access_subnets": True}
    offering.save()


def _grant_permissions():
    CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ACCESS_SUBNET)
    CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_ACCESS_SUBNET)
    CustomerRole.OWNER.add_permission(PermissionEnum.DELETE_ACCESS_SUBNET)


def _create_subnet(fixture, inet="192.168.1.5/32", offerings=(), **kwargs):
    subnet = structure_models.AccessSubnet.objects.create(
        customer=fixture.customer, inet=inet, **kwargs
    )
    for offering in offerings:
        models.AccessSubnetOfferingScope.objects.create(
            access_subnet=subnet, offering=offering
        )
    return subnet


@ddt
class AccessSubnetScopeTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        _enable_subnets(self.fixture.offering)
        _grant_permissions()
        self.fixture.resource

    def create(self, user, **payload):
        self.client.force_authenticate(getattr(self.fixture, user))
        body = {
            "customer": structure_factories.CustomerFactory.get_url(
                self.fixture.customer
            ),
            "inet": "192.168.1.5/32",
            **payload,
        }
        return self.client.post(_list_url(), body, format="json")

    def test_entry_can_be_scoped_to_an_offering(self):
        response = self.create("owner", offerings=[self.fixture.offering.uuid.hex])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["offerings"], [self.fixture.offering.uuid.hex])
        self.assertFalse(response.data["applies_to_portal"])

    def test_entry_can_be_scoped_to_the_portal(self):
        response = self.create("owner", applies_to_portal=True)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["applies_to_portal"])
        self.assertEqual(response.data["offerings"], [])

    def test_entry_can_carry_both_scopes(self):
        response = self.create(
            "owner",
            applies_to_portal=True,
            offerings=[self.fixture.offering.uuid.hex],
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["applies_to_portal"])
        self.assertEqual(response.data["offerings"], [self.fixture.offering.uuid.hex])

    def test_portal_scope_is_off_by_default(self):
        # The whole point of the flag: adding a network to reach a bucket must
        # not silently restrict who can sign in.
        response = self.create("owner")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(response.data["applies_to_portal"])

    def test_offering_the_customer_does_not_consume_is_rejected(self):
        other = fixtures.MarketplaceFixture()
        _enable_subnets(other.offering)
        response = self.create("owner", offerings=[other.offering.uuid.hex])
        self.assertEqual(response.status_code, 400, response.data)

    def test_offering_without_the_plugin_option_is_rejected(self):
        self.fixture.offering.plugin_options = {}
        self.fixture.offering.save()
        response = self.create("owner", offerings=[self.fixture.offering.uuid.hex])
        self.assertEqual(response.status_code, 400, response.data)

    def test_unknown_offering_is_rejected(self):
        response = self.create("owner", offerings=["0" * 32])
        self.assertEqual(response.status_code, 400, response.data)

    def test_scopes_can_be_replaced_on_update(self):
        subnet = _create_subnet(self.fixture, offerings=[self.fixture.offering])
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(
            _detail_url(subnet), {"offerings": []}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["offerings"], [])
        self.assertFalse(
            models.AccessSubnetOfferingScope.objects.filter(
                access_subnet=subnet
            ).exists()
        )

    def test_omitting_offerings_on_update_leaves_scopes_alone(self):
        subnet = _create_subnet(self.fixture, offerings=[self.fixture.offering])
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(
            _detail_url(subnet), {"description": "renamed"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["offerings"], [self.fixture.offering.uuid.hex])


@ddt
class AccessSubnetMaskPolicyTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        _grant_permissions()

    def create(self, user, inet):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.post(
            _list_url(),
            {
                "customer": structure_factories.CustomerFactory.get_url(
                    self.fixture.customer
                ),
                "inet": inet,
            },
            format="json",
        )

    def test_bare_address_becomes_a_single_host(self):
        response = self.create("owner", "192.168.1.5")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["inet"], "192.168.1.5/32")

    def test_non_staff_cannot_enter_wider_than_a_single_host(self):
        self.assertEqual(self.create("owner", "192.168.1.0/24").status_code, 400)

    def test_staff_can_enter_a_wider_network(self):
        response = self.create("staff", "192.168.1.0/24")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["is_staff_managed"])

    def test_zero_prefix_is_rejected_even_for_staff(self):
        self.assertEqual(self.create("staff", "0.0.0.0/0").status_code, 400)

    def test_host_bits_are_rejected_rather_than_masked(self):
        self.assertEqual(self.create("staff", "192.168.1.5/24").status_code, 400)

    def test_consumer_cannot_modify_a_staff_managed_entry(self):
        subnet = _create_subnet(self.fixture, inet="10.0.1.0/24", is_staff_managed=True)
        self.client.force_authenticate(self.fixture.owner)
        self.assertEqual(
            self.client.patch(_detail_url(subnet), {"description": "x"}).status_code,
            400,
        )
        self.assertEqual(self.client.delete(_detail_url(subnet)).status_code, 400)

    def test_staff_widening_an_entry_takes_ownership(self):
        subnet = _create_subnet(self.fixture, inet="10.3.0.1/32")
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(_detail_url(subnet), {"inet": "10.3.0.0/24"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["is_staff_managed"])


class PortalScopeEnforcementTest(test.APITestCase):
    """Only portal-scoped entries may restrict signing in."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        _enable_subnets(self.fixture.offering)
        self.fixture.resource
        self.url = reverse("customer-list")

    def visible_customers(self, ip):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url, HTTP_X_FORWARDED_FOR=ip)
        self.assertEqual(response.status_code, 200, response.data)
        return [item["uuid"] for item in response.data]

    def test_customer_without_portal_entries_is_unrestricted(self):
        self.assertIn(self.fixture.customer.uuid.hex, self.visible_customers("8.8.8.8"))

    def test_offering_scoped_entry_does_not_restrict_sign_in(self):
        # The regression this design exists to prevent: an address added to
        # reach a bucket must not lock the organization out of the portal.
        _create_subnet(
            self.fixture,
            inet="100.100.100.100/32",
            offerings=[self.fixture.offering],
        )
        self.assertIn(self.fixture.customer.uuid.hex, self.visible_customers("8.8.8.8"))

    def test_portal_scoped_entry_restricts_sign_in(self):
        _create_subnet(self.fixture, inet="100.100.100.100/32", applies_to_portal=True)
        self.assertNotIn(
            self.fixture.customer.uuid.hex, self.visible_customers("8.8.8.8")
        )
        self.assertIn(
            self.fixture.customer.uuid.hex,
            self.visible_customers("100.100.100.100"),
        )

    def test_customer_is_not_duplicated_when_several_entries_match(self):
        # The old join-based filter returned one row per matching subnet.
        _create_subnet(self.fixture, inet="100.100.100.100/32", applies_to_portal=True)
        _create_subnet(self.fixture, inet="100.100.100.0/24", applies_to_portal=True)
        visible = self.visible_customers("100.100.100.100")
        self.assertEqual(visible.count(self.fixture.customer.uuid.hex), 1)


ALLOWED_IP = "100.100.100.100"
DENIED_IP = "203.0.113.7"


@ddt
class AccessSubnetConcealmentTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self._enable_conceal(self.fixture.offering)
        self.resource = self.fixture.resource
        _create_subnet(
            self.fixture,
            inet=f"{ALLOWED_IP}/32",
            offerings=[self.fixture.offering],
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_RESOURCES)
        self.consumer = self.fixture.owner

    def _enable_conceal(self, offering, value=True):
        offering.plugin_options = {"conceal_subnet_restricted_resources": value}
        offering.save()

    def _consumer_list_uuids(self, user, ip):
        self.client.force_authenticate(user)
        response = self.client.get(
            reverse("marketplace-resource-list"), HTTP_X_FORWARDED_FOR=ip
        )
        self.assertEqual(response.status_code, 200, response.data)
        return [item["uuid"] for item in response.data]

    def test_hidden_when_address_not_allowed(self):
        self.assertNotIn(
            self.resource.uuid.hex, self._consumer_list_uuids(self.consumer, DENIED_IP)
        )

    def test_visible_when_address_allowed(self):
        self.assertIn(
            self.resource.uuid.hex, self._consumer_list_uuids(self.consumer, ALLOWED_IP)
        )

    def test_visible_when_concealment_disabled(self):
        self._enable_conceal(self.fixture.offering, value=False)
        self.assertIn(
            self.resource.uuid.hex, self._consumer_list_uuids(self.consumer, DENIED_IP)
        )

    def test_unscoped_entry_does_not_conceal(self):
        # An entry that applies only to the portal must not restrict resources.
        models.AccessSubnetOfferingScope.objects.all().delete()
        self.assertIn(
            self.resource.uuid.hex, self._consumer_list_uuids(self.consumer, DENIED_IP)
        )

    def test_scope_applies_to_resources_created_later(self):
        newcomer = factories.ResourceFactory(
            offering=self.fixture.offering,
            plan=self.fixture.plan,
            project=self.fixture.project,
        )
        self.assertNotIn(
            newcomer.uuid.hex, self._consumer_list_uuids(self.consumer, DENIED_IP)
        )
        self.assertIn(
            newcomer.uuid.hex, self._consumer_list_uuids(self.consumer, ALLOWED_IP)
        )

    def test_one_organization_list_does_not_restrict_another(self):
        other = fixtures.MarketplaceFixture()
        other_resource = factories.ResourceFactory(
            offering=self.fixture.offering,
            plan=self.fixture.plan,
            project=other.project,
        )
        self.assertIn(
            other_resource.uuid.hex,
            self._consumer_list_uuids(other.owner, DENIED_IP),
        )

    @data("staff", "global_support")
    def test_staff_and_support_bypass(self, user):
        self.assertIn(
            self.resource.uuid.hex,
            self._consumer_list_uuids(getattr(self.fixture, user), DENIED_IP),
        )

    def test_offering_default_widens_allow_list(self):
        models.AccessSubnetOfferingScope.objects.all().delete()
        models.OfferingAccessSubnet.objects.create(
            offering=self.fixture.offering, inet="203.0.113.0/24"
        )
        self.assertIn(
            self.resource.uuid.hex,
            self._consumer_list_uuids(self.consumer, "203.0.113.50"),
        )
        self.assertNotIn(
            self.resource.uuid.hex,
            self._consumer_list_uuids(self.consumer, "8.8.8.8"),
        )


class ConsumerCustomerOfferingFilterTest(test.APITestCase):
    """The offering filter backing the scope picker.

    Anything it offers must be something the serializer will accept.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        _enable_subnets(self.fixture.offering)
        self.fixture.resource
        self.url = reverse("marketplace-provider-offering-list")

    def list_for(self, customer):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"consumer_customer_uuid": customer.uuid.hex}
        )
        self.assertEqual(response.status_code, 200, response.data)
        return {item["uuid"] for item in response.data}

    def test_returns_consumed_offerings(self):
        self.assertIn(
            self.fixture.offering.uuid.hex, self.list_for(self.fixture.customer)
        )

    def test_excludes_offerings_without_the_plugin_option(self):
        # Without this the picker offered an option whose creation then failed.
        self.fixture.offering.plugin_options = {}
        self.fixture.offering.save()
        self.assertNotIn(
            self.fixture.offering.uuid.hex, self.list_for(self.fixture.customer)
        )

    def test_excludes_offerings_consumed_by_other_customers(self):
        other = fixtures.MarketplaceFixture()
        _enable_subnets(other.offering)
        other.resource
        self.assertNotIn(other.offering.uuid.hex, self.list_for(self.fixture.customer))

    def test_excludes_offerings_with_only_terminated_resources(self):
        self.fixture.resource.state = models.Resource.States.TERMINATED
        self.fixture.resource.save(update_fields=["state"])
        self.assertNotIn(
            self.fixture.offering.uuid.hex, self.list_for(self.fixture.customer)
        )

    def test_provider_customer_is_not_treated_as_consumer(self):
        self.assertNotIn(
            self.fixture.offering.uuid.hex,
            self.list_for(self.fixture.offering.customer),
        )


class AccessSubnetExportTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        _enable_subnets(self.fixture.offering)
        _create_subnet(
            self.fixture, inet="192.168.1.4/32", offerings=[self.fixture.offering]
        )
        _create_subnet(
            self.fixture, inet="192.168.1.5/32", offerings=[self.fixture.offering]
        )

    def test_command_dumps_scoped_subnets(self):
        out = _CommandCapture()
        call_command("resource_access_subnets", stdout=out)
        self.assertIn("192.168.1.4/31", out.value)

    def test_unscoped_subnet_is_not_exported(self):
        # A portal-only entry governs sign-in, not reaching a backend, so it has
        # no business in a firewall allow-list.
        _create_subnet(self.fixture, inet="10.9.9.9/32", applies_to_portal=True)
        out = _CommandCapture()
        call_command(
            "resource_access_subnets",
            offering=self.fixture.offering.uuid.hex,
            stdout=out,
        )
        self.assertNotIn("10.9.9.9/32", out.value)

    def test_provider_defaults_are_exported_alongside_consumer_entries(self):
        models.OfferingAccessSubnet.objects.create(
            offering=self.fixture.offering, inet="198.51.100.0/24"
        )
        out = _CommandCapture()
        call_command(
            "resource_access_subnets",
            offering=self.fixture.offering.uuid.hex,
            stdout=out,
        )
        self.assertIn("198.51.100.0/24", out.value)

    def test_sign_in_export_excludes_offering_only_entries(self):
        # The two lists share a table now, so an unfiltered dump would widen the
        # sign-in allow-list with addresses only ever trusted for resources.
        _create_subnet(self.fixture, inet="10.7.7.7/32", applies_to_portal=True)
        out = _CommandCapture()
        call_command("organization_access_subnets", stdout=out)
        self.assertIn("10.7.7.7/32", out.value)
        self.assertNotIn("192.168.1.4/32", out.value)
        self.assertNotIn("192.168.1.5/32", out.value)

    def test_organization_subnets_merged_on_demand_are_sign_in_only(self):
        _create_subnet(self.fixture, inet="10.8.8.8/32", applies_to_portal=True)
        _create_subnet(self.fixture, inet="10.9.9.9/32")
        out = _CommandCapture()
        call_command(
            "resource_access_subnets",
            offering=self.fixture.offering.uuid.hex,
            include_organization_subnets=True,
            stdout=out,
        )
        self.assertIn("10.8.8.8/32", out.value)
        self.assertNotIn("10.9.9.9/32", out.value)

    def test_offering_endpoint_reports_customer_context(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            reverse(
                "marketplace-provider-offering-access-subnets",
                kwargs={"uuid": self.fixture.offering.uuid.hex},
            )
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = response.data["expanded"][0]
        self.assertEqual(row["customer_uuid"], self.fixture.customer.uuid.hex)
        self.assertEqual(row["offering_uuid"], self.fixture.offering.uuid.hex)
        self.assertEqual(response.data["packed"], ["192.168.1.4/31"])


class AccessSubnetAuditLogTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        _enable_subnets(self.fixture.offering)
        self.subnet = _create_subnet(self.fixture, description="asd")
        self.subnet.refresh_from_db()

    def test_scoping_an_entry_is_logged(self):
        models.AccessSubnetOfferingScope.objects.create(
            access_subnet=self.subnet, offering=self.fixture.offering
        )
        events = logging_models.Event.objects.filter(
            event_type=EventType.ACCESS_SUBNET_UPDATE_SUCCEEDED.value
        )
        self.assertTrue(
            any(self.fixture.offering.name in event.message for event in events)
        )

    def test_unscoping_an_entry_is_logged(self):
        scope = models.AccessSubnetOfferingScope.objects.create(
            access_subnet=self.subnet, offering=self.fixture.offering
        )
        scope.delete()
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type=EventType.ACCESS_SUBNET_UPDATE_SUCCEEDED.value,
                message__contains="no longer applies",
            ).exists()
        )


class DormantScopeTest(test.APITestCase):
    """What happens once the organization terminates its last resource.

    The scope is kept so re-provisioning restores protection without
    reconfiguration, but it must stop being exported and must not make the
    entry uneditable.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        _enable_subnets(self.fixture.offering)
        _grant_permissions()
        self.resource = self.fixture.resource
        self.subnet = _create_subnet(
            self.fixture, inet="10.5.0.1/32", offerings=[self.fixture.offering]
        )

    def terminate(self):
        self.resource.state = models.Resource.States.TERMINATED
        self.resource.save(update_fields=["state"])

    def test_scope_survives_termination(self):
        self.terminate()
        self.assertTrue(
            models.AccessSubnetOfferingScope.objects.filter(
                access_subnet=self.subnet
            ).exists()
        )

    def test_entry_stays_editable(self):
        # Re-validating an unchanged scope rejected every later edit, including
        # the one that would have removed it — leaving no way out.
        self.terminate()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(
            _detail_url(self.subnet),
            {"offerings": [self.fixture.offering.uuid.hex]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_dormant_scope_can_be_removed(self):
        self.terminate()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(
            _detail_url(self.subnet), {"offerings": []}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["offerings"], [])

    def test_adding_a_new_dormant_offering_is_still_rejected(self):
        # The exemption covers scopes that already exist, not new ones.
        other = fixtures.MarketplaceFixture()
        _enable_subnets(other.offering)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(
            _detail_url(self.subnet),
            {"offerings": [self.fixture.offering.uuid.hex, other.offering.uuid.hex]},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)

    def test_dormant_scope_leaves_the_firewall_export(self):
        out = _CommandCapture()
        call_command("resource_access_subnets", stdout=out)
        self.assertIn("10.5.0.1/32", out.value)

        self.terminate()
        out = _CommandCapture()
        call_command("resource_access_subnets", stdout=out)
        self.assertNotIn("10.5.0.1/32", out.value)

    def test_dormant_scope_leaves_the_offering_endpoint(self):
        self.terminate()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            reverse(
                "marketplace-provider-offering-access-subnets",
                kwargs={"uuid": self.fixture.offering.uuid.hex},
            )
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["expanded"], [])
        self.assertEqual(response.data["packed"], [])

    def test_reprovisioning_restores_the_export(self):
        self.terminate()
        factories.ResourceFactory(
            offering=self.fixture.offering,
            plan=self.fixture.plan,
            project=self.fixture.project,
        )
        out = _CommandCapture()
        call_command("resource_access_subnets", stdout=out)
        self.assertIn("10.5.0.1/32", out.value)


class ResourceImpactTest(test.APITestCase):
    """Which resources each address reaches, and whether that is enforced."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        _enable_subnets(self.fixture.offering)
        self.resource = self.fixture.resource
        self.subnet = _create_subnet(
            self.fixture, inet="10.1.1.1/32", offerings=[self.fixture.offering]
        )

    def url(self):
        return reverse("access-subnets-resource-impact")

    def get(self, user=None, **params):
        self.client.force_authenticate(user or self.fixture.staff)
        return self.client.get(
            self.url(),
            {"customer_uuid": self.fixture.customer.uuid.hex, **params},
        )

    def rows(self, **params):
        response = self.get(**params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["resources"]

    def test_resource_lists_the_addresses_that_reach_it(self):
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resource_uuid"], self.resource.uuid.hex)
        self.assertEqual(
            [address["inet"] for address in rows[0]["addresses"]], ["10.1.1.1/32"]
        )
        self.assertEqual(rows[0]["addresses"][0]["source"], "organization")

    def test_provider_defaults_are_reported_separately(self):
        # A consumer needs to know why an address it never added is on the list.
        models.OfferingAccessSubnet.objects.create(
            offering=self.fixture.offering, inet="203.0.113.0/24"
        )
        sources = {
            address["source"]: address["inet"]
            for address in self.rows()[0]["addresses"]
        }
        self.assertEqual(sources["organization"], "10.1.1.1/32")
        self.assertEqual(sources["provider_default"], "203.0.113.0/24")

    def test_advisory_unless_the_offering_conceals(self):
        self.assertFalse(self.rows()[0]["concealment_enabled"])
        self.fixture.offering.plugin_options = {
            "enable_resource_access_subnets": True,
            "conceal_subnet_restricted_resources": True,
        }
        self.fixture.offering.save()
        self.assertTrue(self.rows()[0]["concealment_enabled"])

    def test_resource_with_no_addresses_is_flagged_unrestricted(self):
        # The fail-open case the redesign exists to surface.
        models.AccessSubnetOfferingScope.objects.all().delete()
        row = self.rows()[0]
        self.assertTrue(row["unrestricted"])
        self.assertEqual(row["addresses"], [])

    def test_offering_without_support_is_omitted(self):
        # Nothing can protect it and no allow-list can apply, so listing it
        # would only bury the resources whose exposure is actually in question.
        models.AccessSubnetOfferingScope.objects.all().delete()
        self.fixture.offering.plugin_options = {}
        self.fixture.offering.save()
        self.assertEqual(self.rows(), [])

    def test_unsupported_offering_does_not_hide_a_supported_one(self):
        # The exclusion is per resource, not "any unsupported offering empties
        # the report".
        other = factories.OfferingFactory(customer=self.fixture.offering.customer)
        other.plugin_options = {}
        other.save()
        factories.ResourceFactory(offering=other, project=self.fixture.project)
        rows = self.rows()
        self.assertEqual(
            [row["offering_uuid"] for row in rows], [self.fixture.offering.uuid.hex]
        )

    def test_packed_merges_adjacent_addresses(self):
        _create_subnet(
            self.fixture, inet="10.1.1.0/32", offerings=[self.fixture.offering]
        )
        self.assertEqual(self.rows()[0]["packed"], ["10.1.1.0/31"])

    def test_narrowing_to_one_address_reports_only_what_it_reaches(self):
        other = factories.OfferingFactory(customer=self.fixture.offering.customer)
        _enable_subnets(other)
        factories.ResourceFactory(offering=other, project=self.fixture.project)
        # Two resources overall, but this address is scoped to one offering.
        self.assertEqual(len(self.rows()), 2)
        rows = self.rows(access_subnet_uuid=self.subnet.uuid.hex)
        self.assertEqual(
            [row["offering_uuid"] for row in rows], [self.fixture.offering.uuid.hex]
        )

    def test_owner_may_see_their_own_organization(self):
        # The regression that shipped: comparing a Customer against a queryset
        # of ids is silently False, so this returned 403 for every non-staff
        # caller while staff kept working.
        response = self.get(self.fixture.owner)
        self.assertEqual(response.status_code, 200, response.data)

    def test_outsider_is_denied(self):
        other = fixtures.MarketplaceFixture()
        self.client.force_authenticate(other.owner)
        response = self.client.get(
            self.url(), {"customer_uuid": self.fixture.customer.uuid.hex}
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_customer_uuid_is_required_and_validated(self):
        self.client.force_authenticate(self.fixture.staff)
        self.assertEqual(self.client.get(self.url()).status_code, 400)
        self.assertEqual(
            self.client.get(self.url(), {"customer_uuid": "nonsense"}).status_code, 400
        )

    def test_terminated_resources_are_omitted(self):
        self.resource.state = models.Resource.States.TERMINATED
        self.resource.save(update_fields=["state"])
        self.assertEqual(self.rows(), [])


class AccessSubnetOrderingTest(test.APITestCase):
    """Ordering, including the per-offering columns."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        _enable_subnets(self.fixture.offering)
        self.fixture.resource
        self.scoped = _create_subnet(
            self.fixture, inet="10.0.0.2/32", offerings=[self.fixture.offering]
        )
        self.unscoped = _create_subnet(self.fixture, inet="10.0.0.1/32")

    def order(self, term):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            _list_url(),
            {"customer_uuid": self.fixture.customer.uuid.hex, "o": term},
        )
        self.assertEqual(response.status_code, 200, response.data)
        return [row["inet"] for row in response.data]

    def test_orders_by_address(self):
        self.assertEqual(self.order("inet"), ["10.0.0.1/32", "10.0.0.2/32"])

    def test_orders_by_offering_scope(self):
        key = f"offering:{self.fixture.offering.uuid.hex}"
        # Booleans sort false-first, so the scoped entry lands last ascending.
        self.assertEqual(self.order(key), ["10.0.0.1/32", "10.0.0.2/32"])
        self.assertEqual(self.order(f"-{key}"), ["10.0.0.2/32", "10.0.0.1/32"])

    def test_unknown_field_is_ignored_rather_than_applied(self):
        self.assertEqual(len(self.order("bogus_field")), 2)

    def test_malformed_offering_key_does_not_error(self):
        self.assertEqual(len(self.order("offering:not-a-uuid")), 2)


class _CommandCapture:
    def __init__(self):
        self.value = ""

    def write(self, msg, *args, **kwargs):
        self.value += str(msg) + "\n"
