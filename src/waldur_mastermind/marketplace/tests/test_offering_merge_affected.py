from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from rest_framework import status, test

from waldur_core.permissions import models as permission_models
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.tests import factories as permission_factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.marketplace import models, offering_merge, offering_merge_rows
from waldur_mastermind.marketplace import offering_merge_coverage as coverage
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.test_offering_merge import (
    TwoSupportOfferingsScenario,
)
from waldur_mastermind.marketplace.tests.test_offering_merge_api import merge_url

States = models.OfferingMerge.States


class OfferingMergePreviewEntriesTest(TwoSupportOfferingsScenario, TestCase):
    """The preview says what each registry entry is about and what happens to it."""

    def test_every_entry_reports_its_area_and_effect_beside_the_count(self):
        preview = offering_merge.build_preview(self.make_merge())

        entries = {entry["label"]: entry for entry in preview["entries"]}
        self.assertEqual(set(entries), set(preview["counts"]))
        for label, entry in entries.items():
            self.assertEqual(entry["count"], preview["counts"][label], label)
            self.assertIn(entry["area"], coverage.AREAS, label)
            self.assertIn(entry["effect"], coverage.EFFECTS, label)
            self.assertTrue(entry["area_title"], label)
            self.assertTrue(entry["effect_title"], label)

        resources = entries["marketplace.Resource.offering"]
        self.assertEqual(resources["area"], coverage.AREA_RESOURCES_AND_ORDERS)
        self.assertEqual(resources["area_title"], "Resources and orders")
        self.assertEqual(resources["effect"], coverage.EFFECT_MOVED)
        self.assertEqual(resources["count"], 2)
        self.assertTrue(resources["can_list_rows"])

        self.assertEqual(
            entries["marketplace.ComponentUsage.component"]["area"],
            coverage.AREA_BILLING_HISTORY,
        )
        self.assertEqual(
            entries["invoices.InvoiceItem.details"]["effect"],
            coverage.EFFECT_REWRITTEN,
        )
        self.assertEqual(
            entries["marketplace.OfferingUser.offering"]["effect"],
            coverage.EFFECT_DEDUPLICATED,
        )
        self.assertEqual(
            entries["marketplace.Plan.offering"]["effect"],
            coverage.EFFECT_KEPT_ON_SOURCE,
        )

        summaries = entries["marketplace.ComponentUsageMonthly.component"]
        self.assertEqual(summaries["effect"], coverage.EFFECT_RECOMPUTED)
        self.assertFalse(summaries["can_list_rows"])

    def test_the_counts_mapping_keeps_its_shape(self):
        preview = offering_merge.build_preview(self.make_merge())

        counts = preview["counts"]
        self.assertTrue(all(isinstance(value, int) for value in counts.values()))
        self.assertEqual(counts["marketplace.Resource.offering"], 2)
        self.assertEqual(counts["marketplace.Order.offering"], 1)
        self.assertEqual(counts["marketplace.OfferingUser.offering"], 2)
        # Not applicable entries are still absent, as before.
        self.assertNotIn("marketplace.OfferingMerge.target", counts)

    def test_an_entry_reports_the_rows_that_stay_on_the_source(self):
        # The target already has an account for the source's user, so that one
        # account cannot be repointed.
        factories.OfferingUserFactory(
            offering=self.target, user=self.offering_user_a.user
        )

        preview = offering_merge.build_preview(self.make_merge())

        entries = {entry["label"]: entry for entry in preview["entries"]}
        accounts = entries["marketplace.OfferingUser.offering"]
        # The count is of the sources' accounts; the target's own is not one.
        self.assertEqual(accounts["count"], 2)
        self.assertEqual(accounts["left_on_source"], 1)
        self.assertEqual(
            preview["left_on_source"]["marketplace.OfferingUser.offering"], 1
        )


class OfferingMergeAffectedRowsTest(TwoSupportOfferingsScenario, TestCase):
    """The engine lists the rows of one entry without writing anything."""

    def rows(self, label):
        entry = coverage.MERGE_COVERAGE[label]
        source = offering_merge_rows.affected_rows(self.make_merge(), entry)
        return source.describe(list(source.items))

    def test_a_moved_row_names_the_offering_it_leaves_and_joins(self):
        rows = self.rows("marketplace.Resource.offering")

        self.assertEqual(
            [row.id for row in rows], [self.resource_b.id, self.resource_a.id]
        )
        row = rows[-1]
        self.assertEqual(row.uuid, self.resource_a.uuid.hex)
        self.assertEqual(row.model, "marketplace.Resource")
        self.assertEqual(row.field, "offering_id")
        self.assertEqual(row.old_value, self.source_a.name)
        self.assertEqual(row.new_value, self.target.name)
        self.assertFalse(row.kept_on_source)
        self.assertIn(self.resource_a.name, row.description)
        self.assertIn(self.resource_a.project.name, row.description)
        self.assertIn(self.a_plans["basic"].name, row.description)

    def test_a_rewritten_document_shows_only_the_keys_that_change(self):
        rows = {row.id: row for row in self.rows("marketplace.Resource.limits")}

        row = rows[self.resource_a.id]
        self.assertEqual(row.field, "limits")
        self.assertEqual(row.old_value, "cpu: 2")
        self.assertEqual(row.new_value, "vcpu: 2")
        self.assertFalse(row.kept_on_source)

    def test_an_invoice_item_is_described_by_its_invoice_and_amount(self):
        rows = {row.id: row for row in self.rows("invoices.InvoiceItem.details")}

        row = rows[self.invoice_item.id]
        invoice = self.invoice_item.invoice
        self.assertEqual(row.uuid, self.invoice_item.uuid.hex)
        self.assertIn(f"Invoice {invoice.number}", row.description)
        self.assertIn(invoice.customer.name, row.description)
        self.assertIn(self.resource_a.name, row.description)
        self.assertIn("cpu", row.description)
        self.assertIn(f"{invoice.year:04d}-{invoice.month:02d}", row.description)
        self.assertIn(str(self.invoice_item.price), row.description)
        self.assertEqual(row.old_value, "offering_component_type: cpu")
        self.assertEqual(row.new_value, "offering_component_type: vcpu")

    def test_an_invoice_items_plan_component_is_named_not_numbered(self):
        rows = {row.id: row for row in self.rows("invoices.InvoiceItem.plan_component")}

        row = rows[self.invoice_item.id]
        self.assertEqual(row.field, "plan_component_id")
        self.assertEqual(row.old_value, "cpu (basic)")
        self.assertEqual(row.new_value, "vcpu (Tier 1)")

    def source_plan_component(self):
        return models.PlanComponent.objects.get(plan=self.a_plans["basic"])

    def snapshot_item(self, details):
        return invoice_factories.InvoiceItemFactory(
            resource=self.resource_a,
            project=self.resource_a.project,
            plan_component=self.source_plan_component(),
            details=details,
        )

    def test_a_rewritten_snapshot_shows_the_names_and_drops_the_identifiers(self):
        plan = self.a_plans["basic"]
        item = self.snapshot_item(
            {
                "offering_uuid": self.source_a.uuid.hex,
                "offering_name": self.source_a.name,
                "plan_uuid": plan.uuid.hex,
                "plan_name": plan.name,
                "plan_component_id": self.source_plan_component().id,
            }
        )

        rows = {row.id: row for row in self.rows("invoices.InvoiceItem.details")}

        row = rows[item.id]
        self.assertEqual(
            row.old_value,
            f"offering_name: {self.source_a.name}, plan_name: basic",
        )
        self.assertEqual(
            row.new_value,
            f"offering_name: {self.target.name}, plan_name: Tier 1",
        )
        # The uuids and row ids change too, and say nothing the names do not.
        for value in (row.old_value, row.new_value):
            self.assertNotIn("uuid", value)
            self.assertNotIn("plan_component_id", value)

    def test_an_identifier_that_changes_alone_is_still_shown(self):
        source_component = self.source_plan_component()
        target_component = models.PlanComponent.objects.get(
            plan=self.target_plans["Tier 1"], component__type="vcpu"
        )
        item = self.snapshot_item({"plan_component_id": source_component.id})

        rows = {row.id: row for row in self.rows("invoices.InvoiceItem.details")}

        row = rows[item.id]
        self.assertEqual(row.old_value, f"plan_component_id: {source_component.id}")
        self.assertEqual(row.new_value, f"plan_component_id: {target_component.id}")

    def test_usage_plan_period_order_and_account_rows_are_described(self):
        descriptions = {
            "marketplace.ComponentUsage.component": (
                self.usage_a.id,
                (self.resource_a.name, "cpu"),
            ),
            "marketplace.ResourcePlanPeriod.plan": (
                models.ResourcePlanPeriod.objects.get(resource=self.resource_a).id,
                (self.resource_a.name, "basic"),
            ),
            "marketplace.Order.offering": (
                self.order_a.id,
                (self.resource_a.name, "Create", "done"),
            ),
            "marketplace.OfferingUser.offering": (
                self.offering_user_a.id,
                (self.offering_user_a.username, self.source_a.name),
            ),
        }
        for label, (pk, expected) in descriptions.items():
            with self.subTest(label=label):
                rows = {row.id: row for row in self.rows(label)}
                description = rows[pk].description
                for part in expected:
                    self.assertIn(part, description)
                self.assertNotEqual(description, str(pk))

    def test_rows_kept_on_the_source_are_not_presented_as_moving(self):
        factories.OfferingUserFactory(
            offering=self.target, user=self.offering_user_a.user
        )

        rows = {row.id: row for row in self.rows("marketplace.OfferingUser.offering")}

        kept = rows[self.offering_user_a.id]
        self.assertTrue(kept.kept_on_source)
        self.assertIsNone(kept.new_value)
        self.assertEqual(kept.old_value, self.source_a.name)
        moved = rows[self.offering_user_b.id]
        self.assertFalse(moved.kept_on_source)
        self.assertEqual(moved.new_value, self.target.name)

    def test_configuration_that_stays_behind_is_listed_as_kept(self):
        rows = self.rows("marketplace.Plan.offering")

        self.assertEqual(
            {row.id for row in rows},
            {self.a_plans["basic"].id, self.b_plans["premium"].id},
        )
        for row in rows:
            self.assertTrue(row.kept_on_source)
            self.assertIsNone(row.new_value)
            self.assertIn(row.old_value, {self.source_a.name, self.source_b.name})

    def test_offering_configuration_rows_say_what_they_are_and_whose(self):
        terms = models.OfferingTermsOfService.objects.create(
            offering=self.source_a, version="2"
        )
        screenshot = factories.ScreenshotFactory(
            offering=self.source_a, name="front page"
        )
        offering_file = factories.OfferingFileFactory(
            offering=self.source_a, name="user guide"
        )
        endpoint = models.OfferingAccessEndpoint.objects.create(
            offering=self.source_a, name="dashboard", url="https://example.com/"
        )
        expected = {
            "marketplace.OfferingTermsOfService.offering": (
                terms.id,
                f"Terms of service for {self.source_a.name}, version 2",
            ),
            "marketplace.Plan.offering": (
                self.a_plans["basic"].id,
                f"Plan basic of {self.source_a.name}",
            ),
            "marketplace.OfferingComponent.offering": (
                self.a_components["cpu"].id,
                f"Component cpu (cpu) of {self.source_a.name}",
            ),
            "marketplace.Screenshot.offering": (
                screenshot.id,
                f"Screenshot front page of {self.source_a.name}",
            ),
            "marketplace.OfferingFile.offering": (
                offering_file.id,
                f"File user guide of {self.source_a.name}",
            ),
            "marketplace.OfferingAccessEndpoint.offering": (
                endpoint.id,
                f"Access endpoint dashboard (https://example.com/) "
                f"of {self.source_a.name}",
            ),
        }
        for label, (pk, description) in expected.items():
            with self.subTest(label=label):
                rows = {row.id: row for row in self.rows(label)}
                self.assertEqual(rows[pk].description, description)

    def test_a_price_list_row_names_its_component_plan_and_offering(self):
        plan_component = models.PlanComponent.objects.get(plan=self.a_plans["basic"])

        rows = {row.id: row for row in self.rows("marketplace.PlanComponent.plan")}

        self.assertEqual(
            rows[plan_component.id].description,
            f"Price of cpu in plan basic of {self.source_a.name}",
        )

    def test_a_quota_row_names_the_resource_component_and_limit(self):
        rows = {
            row.id: row for row in self.rows("marketplace.ComponentQuota.component")
        }

        # The limit is read back as the column stores it, to two decimals.
        self.quota_b.refresh_from_db()
        self.assertEqual(
            rows[self.quota_b.id].description,
            f"Quota of ram for {self.resource_b.name}, limit {self.quota_b.limit}",
        )
        self.assertIn("limit 8", rows[self.quota_b.id].description)

    def test_a_consent_row_names_the_user_and_the_offering(self):
        user = structure_factories.UserFactory()
        consent = models.UserOfferingConsent.objects.create(
            user=user, offering=self.source_a, version="2"
        )

        rows = {
            row.id: row for row in self.rows("marketplace.UserOfferingConsent.offering")
        }

        self.assertEqual(
            rows[consent.id].description,
            f"Consent of {user.full_name} to the terms of {self.source_a.name}, "
            f"version 2",
        )

    def test_offering_scoped_role_rows_name_the_role_the_user_and_the_offering(self):
        role = permission_factories.RoleFactory(
            name="OFFERING.MANAGER",
            content_type=ContentType.objects.get_for_model(models.Offering),
        )
        user = structure_factories.UserFactory()
        granted = permission_models.UserRole.objects.create(
            user=user, role=role, scope=self.source_a, is_active=True
        )
        available = permission_models.RoleAvailability.objects.create(
            role=role, scope=self.source_a
        )

        granted_rows = {row.id: row for row in self.rows("permissions.UserRole.scope")}
        available_rows = {
            row.id: row for row in self.rows("permissions.RoleAvailability.scope")
        }

        self.assertEqual(
            granted_rows[granted.id].description,
            f"{role.description} for {user.full_name} on {self.source_a.name}",
        )
        self.assertEqual(
            available_rows[available.id].description,
            f"{role.description} offered on {self.source_a.name}",
        )
        # The role's own words, not the machine name staff never see elsewhere.
        self.assertNotIn("OFFERING.MANAGER", granted_rows[granted.id].description)

    def test_a_model_without_a_describer_reads_as_what_it_is_and_whose(self):
        status = factories.IntegrationStatusFactory(offering=self.source_a)

        rows = self.rows("marketplace.IntegrationStatus.offering")

        self.assertEqual([row.id for row in rows], [status.id])
        self.assertEqual(rows[0].uuid, status.uuid.hex)
        self.assertEqual(
            rows[0].description, f"Integration status of {self.source_a.name}"
        )
        # Not the Django repr the fallback used to hand back.
        self.assertNotIn("object (", rows[0].description)

    def test_describing_a_page_does_not_query_per_row(self):
        for _ in range(4):
            invoice_factories.InvoiceItemFactory(
                resource=self.resource_a,
                project=self.resource_a.project,
                plan_component=models.PlanComponent.objects.get(
                    plan=self.a_plans["basic"]
                ),
                details={"offering_component_type": "cpu"},
            )
        entry = coverage.MERGE_COVERAGE["invoices.InvoiceItem.details"]
        source = offering_merge_rows.affected_rows(self.make_merge(), entry)
        page = list(source.items)
        self.assertGreaterEqual(len(page), 5)

        # One query for the page of items with its relations joined, and none
        # for the values: a document is rendered from itself.
        with self.assertNumQueries(1):
            rows = source.describe(page)

        self.assertEqual(len(rows), len(page))

    def test_describing_a_page_of_references_resolves_names_in_bulk(self):
        for _ in range(4):
            factories.OfferingUserFactory(offering=self.source_a)
        entry = coverage.MERGE_COVERAGE["marketplace.OfferingUser.offering"]
        source = offering_merge_rows.affected_rows(self.make_merge(), entry)
        page = list(source.items)
        self.assertEqual(len(page), 6)

        # The accounts, and one query resolving every offering id to a name.
        with self.assertNumQueries(2):
            rows = source.describe(page)

        self.assertEqual(len(rows), 6)

    def test_describing_a_page_of_price_list_rows_joins_plan_and_offering(self):
        for name in ("extra-1", "extra-2", "extra-3"):
            plan = factories.PlanFactory(offering=self.source_a, name=name)
            factories.PlanComponentFactory(
                plan=plan, component=self.a_components["cpu"]
            )
        entry = coverage.MERGE_COVERAGE["marketplace.PlanComponent.plan"]
        source = offering_merge_rows.affected_rows(self.make_merge(), entry)
        page = list(source.items)
        self.assertGreaterEqual(len(page), 6)

        # The price list rows with their component, plan and offering joined,
        # and one query naming the plans the values point at.
        with self.assertNumQueries(2):
            rows = source.describe(page)

        self.assertEqual(len(rows), len(page))

    def test_describing_a_page_of_role_rows_resolves_the_scopes_in_bulk(self):
        role = permission_factories.RoleFactory(
            content_type=ContentType.objects.get_for_model(models.Offering)
        )
        for _ in range(4):
            permission_models.UserRole.objects.create(
                user=structure_factories.UserFactory(),
                role=role,
                scope=self.source_a,
                is_active=True,
            )
        entry = coverage.MERGE_COVERAGE["permissions.UserRole.scope"]
        source = offering_merge_rows.affected_rows(self.make_merge(), entry)
        page = list(source.items)
        self.assertEqual(len(page), 4)

        # The grants with their user and role joined, the offerings their
        # generic scopes point at, and the same offerings named as values.
        with self.assertNumQueries(3):
            rows = source.describe(page)

        self.assertEqual(len(rows), 4)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class OfferingMergeAffectedApiTest(TwoSupportOfferingsScenario, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)
        self.owner = structure_factories.UserFactory()
        self.target.customer.add_user(self.owner, CustomerRole.OWNER)
        self.manager = structure_factories.UserFactory()
        self.resource_a.project.add_user(self.manager, ProjectRole.MANAGER)
        self.other = structure_factories.UserFactory()

    def get(self, merge, label, user=None, **params):
        self.client.force_authenticate(user or self.staff)
        return self.client.get(merge_url(merge, "affected"), {"entry": label, **params})

    def previewed(self):
        merge = self.make_merge()
        self.client.force_authenticate(self.staff)
        response = self.client.post(merge_url(merge, "preview"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        merge.refresh_from_db()
        return merge

    def done(self):
        merge = self.previewed()
        response = self.client.post(merge_url(merge, "execute"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        merge.refresh_from_db()
        self.assertEqual(merge.state, States.DONE, merge.error_message)
        return merge

    def test_a_draft_merge_lists_the_rows_it_plans_to_move(self):
        merge = self.make_merge()

        response = self.get(merge, "marketplace.Resource.offering")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response["X-Result-Count"], "2")
        rows = {row["id"]: row for row in response.data}
        row = rows[self.resource_a.id]
        self.assertEqual(row["uuid"], self.resource_a.uuid.hex)
        self.assertEqual(row["model"], "marketplace.Resource")
        self.assertEqual(row["old_value"], self.source_a.name)
        self.assertEqual(row["new_value"], self.target.name)
        self.assertFalse(row["kept_on_source"])
        self.assertIn(self.resource_a.name, row["description"])
        self.assertEqual(merge.state, States.DRAFT)

    def test_a_previewed_merge_lists_both_sources_in_one_answer(self):
        merge = self.previewed()

        response = self.get(merge, "marketplace.Resource.offering")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            {row["old_value"] for row in response.data},
            {self.source_a.name, self.source_b.name},
        )
        self.assertEqual(
            {row["new_value"] for row in response.data}, {self.target.name}
        )

    def test_an_invoice_row_carries_the_invoice_customer_resource_and_amount(self):
        merge = self.previewed()

        response = self.get(merge, "invoices.InvoiceItem.details")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        row = {row["id"]: row for row in response.data}[self.invoice_item.id]
        invoice = self.invoice_item.invoice
        self.assertIn(f"Invoice {invoice.number}", row["description"])
        self.assertIn(invoice.customer.name, row["description"])
        self.assertIn(self.resource_a.name, row["description"])
        self.assertEqual(row["old_value"], "offering_component_type: cpu")
        self.assertEqual(row["new_value"], "offering_component_type: vcpu")

    def test_rows_kept_on_the_source_are_flagged(self):
        factories.OfferingUserFactory(
            offering=self.target, user=self.offering_user_a.user
        )
        merge = self.previewed()

        response = self.get(merge, "marketplace.OfferingUser.offering")

        rows = {row["id"]: row for row in response.data}
        self.assertTrue(rows[self.offering_user_a.id]["kept_on_source"])
        self.assertIsNone(rows[self.offering_user_a.id]["new_value"])
        self.assertFalse(rows[self.offering_user_b.id]["kept_on_source"])

    def test_a_done_merge_is_described_from_its_journal(self):
        merge = self.done()

        response = self.get(merge, "marketplace.Resource.offering")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 2)
        rows = {row["id"]: row for row in response.data}
        row = rows[self.resource_a.id]
        self.assertEqual(row["old_value"], self.source_a.name)
        self.assertEqual(row["new_value"], self.target.name)
        self.assertEqual(
            models.OfferingMergeChange.objects.filter(
                merge=merge, model="marketplace.Resource", field="offering_id"
            ).count(),
            2,
        )

    def test_an_undone_merge_still_describes_what_it_changed(self):
        merge = self.done()
        response = self.client.post(merge_url(merge, "undo"))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        merge.refresh_from_db()
        self.assertEqual(merge.state, States.UNDONE, merge.error_message)

        response = self.get(merge, "marketplace.Resource.offering")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 2)
        self.resource_a.refresh_from_db()
        self.assertEqual(self.resource_a.offering, self.source_a)

    def test_a_done_merge_does_not_recompute_a_plan_for_an_archived_source(self):
        merge = self.done()

        response = self.get(merge, "marketplace.Plan.offering")

        # Plans are kept on the source, so the merge journalled nothing for them.
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data, [])

    def test_the_answer_is_paginated(self):
        merge = self.previewed()

        response = self.get(merge, "marketplace.Resource.offering", page_size=1)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response["X-Result-Count"], "2")
        self.assertEqual(response.data[0]["id"], self.resource_b.id)

        second = self.get(merge, "marketplace.Resource.offering", page_size=1, page=2)
        self.assertEqual(second.data[0]["id"], self.resource_a.id)

    def test_an_unknown_entry_is_refused(self):
        merge = self.previewed()

        for label in ("", "marketplace.Nonexistent.offering", "not a label"):
            with self.subTest(label=label):
                response = self.get(merge, label)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("entry", response.data)

    def test_an_entry_whose_rows_cannot_be_listed_is_refused(self):
        merge = self.previewed()

        response = self.get(merge, "marketplace.ComponentUsageMonthly.component")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("entry", response.data)

    def test_support_reads_it_and_nothing_is_written(self):
        merge = self.make_merge()
        changes = models.OfferingMergeChange.objects.count()
        items = list(
            invoice_models.InvoiceItem.objects.order_by("pk").values_list(
                "pk", "details", "plan_component_id"
            )
        )

        response = self.get(merge, "marketplace.Resource.offering", user=self.support)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 2)
        merge.refresh_from_db()
        self.assertEqual(merge.state, States.DRAFT)
        self.assertEqual(merge.preview, {})
        self.assertEqual(models.OfferingMergeChange.objects.count(), changes)
        self.assertEqual(
            list(
                invoice_models.InvoiceItem.objects.order_by("pk").values_list(
                    "pk", "details", "plan_component_id"
                )
            ),
            items,
        )
        self.resource_a.refresh_from_db()
        self.assertEqual(self.resource_a.offering, self.source_a)

    def test_other_roles_are_refused(self):
        merge = self.previewed()

        for user in (self.owner, self.manager, self.other):
            with self.subTest(user=user):
                response = self.get(merge, "marketplace.Resource.offering", user=user)
                self.assertIn(
                    response.status_code,
                    (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                )
