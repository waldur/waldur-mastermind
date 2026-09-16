from io import StringIO

from django.core.management import call_command
from rest_framework import status

from waldur_core.logging.models import Event
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_sram import models
from waldur_sram.tests import payloads
from waldur_sram.tests.base import SramScimTest


class OrganizationMappingTest(SramScimTest):
    def push_group(self, **kwargs):
        body = payloads.sram_group(**kwargs)
        response = self.sbs.provision("Groups", body)
        return body, response

    def test_unknown_organisation_creates_one_customer(self):
        _, response = self.push_group(urn="uuc:research")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.push_group(urn="uuc:physics")
        self.push_group(urn="uuc:physics:admins")

        customer = Customer.objects.get(backend_id="uuc")
        self.assertEqual(customer.name, "uuc")
        self.assertEqual(
            set(models.SramGroup.objects.values_list("customer_id", flat=True)),
            {customer.id},
        )

    def test_customer_with_matching_name_is_adopted(self):
        existing = structure_factories.CustomerFactory(name="uuc", backend_id="")
        self.push_group(urn="uuc:research")

        existing.refresh_from_db()
        self.assertEqual(existing.backend_id, "uuc")
        self.assertEqual(Customer.objects.filter(name="uuc").count(), 1)
        self.assertEqual(models.SramGroup.objects.get().customer, existing)
        self.assertTrue(
            Event.objects.filter(
                event_type="customer_update_succeeded",
                message__contains="linked to SRAM organisation uuc",
            ).exists()
        )

    def test_customer_with_backend_id_is_reused_after_rename(self):
        existing = structure_factories.CustomerFactory(
            name="University of Utopia", backend_id="uuc"
        )
        structure_factories.CustomerFactory(name="uuc", backend_id="")
        self.push_group(urn="uuc:research")
        self.assertEqual(models.SramGroup.objects.get().customer, existing)

    def test_name_match_with_other_backend_id_is_not_adopted(self):
        other = structure_factories.CustomerFactory(name="uuc", backend_id="crm-1")
        self.push_group(urn="uuc:research")
        customer = models.SramGroup.objects.get().customer
        self.assertNotEqual(customer, other)
        self.assertEqual(customer.backend_id, "uuc")

    def test_ambiguous_name_creates_nothing(self):
        structure_factories.CustomerFactory(name="uuc", backend_id="")
        structure_factories.CustomerFactory(name="uuc", backend_id="")
        _, response = self.push_group(urn="uuc:research")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(models.SramGroup.objects.exists())
        self.assertEqual(Customer.objects.filter(name="uuc").count(), 2)
        self.assertFalse(Customer.objects.filter(backend_id="uuc").exists())

    def test_ambiguous_backend_id_is_a_conflict(self):
        structure_factories.CustomerFactory(backend_id="uuc")
        structure_factories.CustomerFactory(backend_id="uuc")
        _, response = self.push_group(urn="uuc:research")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_group_without_organisation_is_rejected(self):
        _, response = self.push_group(urn="")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_deleting_the_last_group_keeps_the_customer(self):
        body, response = self.push_group(urn="uuc:research")
        self.sbs.delete(response.json())
        self.assertFalse(models.SramGroup.objects.exists())
        self.assertTrue(Customer.objects.filter(backend_id="uuc").exists())

    def test_braces_in_short_name_do_not_break_the_event(self):
        structure_factories.CustomerFactory(name="u{x}c", backend_id="")
        _, response = self.push_group(urn="u{x}c:research")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class ResyncCommandTest(SramScimTest):
    def test_resync_maps_groups_stored_before_the_mapping_existed(self):
        body = payloads.sram_group(urn="uuc:research")
        self.sbs.provision("Groups", body)
        models.SramGroup.objects.update(customer=None)
        Customer.objects.filter(backend_id="uuc").update(backend_id="")

        out = StringIO()
        call_command("sram_resync", stdout=out)

        self.assertIn("Re-applied 1 SRAM groups, 0 failed.", out.getvalue())
        group = models.SramGroup.objects.get()
        self.assertEqual(group.customer.backend_id, "uuc")
        self.assertEqual(Customer.objects.filter(name="uuc").count(), 1)

    def test_resync_reports_failures(self):
        self.sbs.provision("Groups", payloads.sram_group(urn="uuc:research"))
        structure_factories.CustomerFactory(backend_id="uuc")

        out, err = StringIO(), StringIO()
        call_command("sram_resync", stdout=out, stderr=err)

        self.assertIn("1 failed", out.getvalue())
        self.assertIn("Several organizations", err.getvalue())
