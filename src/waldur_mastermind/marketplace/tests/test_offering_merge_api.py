import datetime
from decimal import Decimal
from unittest import mock

from django.db import connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, offering_merge, tasks
from waldur_mastermind.marketplace.enums import BillingTypes, OfferingStates
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.test_offering_merge import (
    TwoSupportOfferingsScenario,
    codes,
    make_merge,
    make_offering,
)

States = models.OfferingMerge.States


def merge_url(merge=None, action=None):
    if merge is None:
        url = reverse("marketplace-offering-merge-list")
    else:
        url = reverse(
            "marketplace-offering-merge-detail", kwargs={"uuid": merge.uuid.hex}
        )
    url = "http://testserver" + url
    return f"{url}{action}/" if action else url


def events(event_type):
    return logging_models.Event.objects.filter(event_type=event_type)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class OfferingMergeApiTest(TwoSupportOfferingsScenario, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)
        self.owner = structure_factories.UserFactory()
        self.target.customer.add_user(self.owner, CustomerRole.OWNER)
        self.manager = structure_factories.UserFactory()
        self.resource_a.project.add_user(self.manager, ProjectRole.MANAGER)
        self.other = structure_factories.UserFactory()

    def payload(self, **overrides):
        payload = {
            "sources": [self.source_a.uuid.hex, self.source_b.uuid.hex],
            "target": self.target.uuid.hex,
            "plan_mapping": {
                source.uuid.hex: target.uuid.hex
                for source, target in self.plan_mapping.items()
            },
            "component_mapping": self.component_mapping,
        }
        payload.update(overrides)
        return payload

    def post(self, merge, action, user=None, data=None):
        # Tests run on_commit callbacks at once and Celery tasks eagerly, so
        # the task has finished when the response arrives.
        self.client.force_authenticate(user or self.staff)
        return self.client.post(merge_url(merge, action), data or {}, format="json")

    def previewed(self, **kwargs):
        merge = self.make_merge(**kwargs)
        response = self.post(merge, "preview")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        merge.refresh_from_db()
        return merge

    def done(self):
        merge = self.previewed()
        response = self.post(merge, "execute")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        merge.refresh_from_db()
        self.assertEqual(merge.state, States.DONE, merge.error_message)
        return merge

    # --- Records ------------------------------------------------------------

    def test_staff_creates_a_draft_owned_by_them(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(merge_url(), self.payload(), format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        merge = models.OfferingMerge.objects.get(uuid=response.data["uuid"])
        self.assertEqual(merge.state, States.DRAFT)
        self.assertEqual(merge.created_by, self.staff)
        self.assertEqual(merge.target, self.target)
        self.assertEqual(set(merge.sources.all()), {self.source_a, self.source_b})
        self.assertEqual(
            merge.plan_mapping,
            {
                self.a_plans["basic"].uuid.hex: self.target_plans["Tier 1"].uuid.hex,
                self.b_plans["premium"].uuid.hex: self.target_plans["Tier 2"].uuid.hex,
            },
        )
        self.assertIsNone(response.data["preview"])
        self.assertEqual(response.data["target_offering"]["name"], self.target.name)

    def test_mapped_plans_and_components_must_belong_to_the_right_offerings(self):
        self.client.force_authenticate(self.staff)
        target_plan = self.target_plans["Tier 1"].uuid.hex
        source_plan = self.a_plans["basic"].uuid.hex
        cases = {
            "plan_mapping": [
                {"plan_mapping": {target_plan: target_plan}},
                {"plan_mapping": {source_plan: source_plan}},
                {"plan_mapping": {"not-a-uuid": target_plan}},
            ],
            "component_mapping": [
                {"component_mapping": {self.target.uuid.hex: {"vcpu": "vcpu"}}},
                {"component_mapping": {self.source_a.uuid.hex: {"ram": "memory"}}},
                {"component_mapping": {self.source_a.uuid.hex: {"cpu": "gpu"}}},
            ],
            "target": [{"sources": [self.target.uuid.hex]}],
        }
        for field, payloads in cases.items():
            for overrides in payloads:
                with self.subTest(overrides=overrides):
                    response = self.client.post(
                        merge_url(), self.payload(**overrides), format="json"
                    )
                    self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                    self.assertIn(field, response.data)

        response = self.client.post(
            merge_url(),
            self.payload(sources=[factories.OfferingFactory().uuid.hex, "0" * 32]),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("sources", response.data)

    def test_editing_a_previewed_merge_returns_it_to_draft(self):
        merge = self.previewed()
        self.assertEqual(merge.state, States.PREVIEWED)

        self.client.force_authenticate(self.staff)
        response = self.client.patch(
            merge_url(merge), {"attribute_key_mapping": {"a": "b"}}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        merge.refresh_from_db()
        self.assertEqual(merge.state, States.DRAFT)
        self.assertEqual(merge.preview, {})
        self.assertEqual(merge.attribute_key_mapping, {"a": "b"})
        self.assertIsNone(response.data["preview"])

    def test_a_merge_past_preview_cannot_be_edited_or_deleted(self):
        merge = self.done()
        self.client.force_authenticate(self.staff)

        response = self.client.patch(
            merge_url(merge), {"attribute_key_mapping": {}}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        response = self.client.delete(merge_url(merge))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(models.OfferingMerge.objects.filter(pk=merge.pk).exists())

    def test_delete_removes_the_record_but_no_offering(self):
        merge = self.previewed()
        self.client.force_authenticate(self.staff)

        response = self.client.delete(merge_url(merge))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.OfferingMerge.objects.filter(pk=merge.pk).exists())
        self.assertEqual(
            models.Offering.objects.filter(
                pk__in=[self.target.pk, self.source_a.pk, self.source_b.pk]
            ).count(),
            3,
        )

    # --- Permissions --------------------------------------------------------

    def test_support_reads_and_previews_without_storing(self):
        merge = self.make_merge()
        self.client.force_authenticate(self.support)

        self.assertEqual(self.client.get(merge_url()).status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.client.get(merge_url(merge)).status_code, status.HTTP_200_OK
        )
        response = self.client.get(
            merge_url(action="suggest_mapping"),
            {"sources": self.source_a.uuid.hex, "target": self.target.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.post(merge, "preview", user=self.support)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["blockers"], [])
        self.assertEqual(response.data["counts"]["marketplace.Resource.offering"], 2)
        merge.refresh_from_db()
        self.assertEqual(merge.state, States.DRAFT)
        self.assertEqual(merge.preview, {})

    def test_support_cannot_write(self):
        merge = self.make_merge()
        self.client.force_authenticate(self.support)
        self.assert_writes_forbidden(merge, status.HTTP_403_FORBIDDEN)

    def test_other_roles_are_refused_everywhere(self):
        merge = self.make_merge()
        for user in (self.owner, self.manager, self.other):
            with self.subTest(user=user):
                self.client.force_authenticate(user)
                self.assertIn(
                    self.client.get(merge_url()).status_code,
                    (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                )
                self.assertIn(
                    self.client.get(merge_url(merge)).status_code,
                    (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                )
                response = self.client.get(
                    merge_url(action="suggest_mapping"),
                    {"sources": self.source_a.uuid.hex, "target": self.target.uuid.hex},
                )
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                response = self.client.post(merge_url(merge, "preview"))
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
                self.assert_writes_forbidden(merge, status.HTTP_403_FORBIDDEN)

    def assert_writes_forbidden(self, merge, expected):
        responses = {
            "create": self.client.post(merge_url(), self.payload(), format="json"),
            "partial_update": self.client.patch(
                merge_url(merge), {"attribute_key_mapping": {}}, format="json"
            ),
            "execute": self.client.post(merge_url(merge, "execute")),
            "undo": self.client.post(merge_url(merge, "undo")),
            "destroy": self.client.delete(merge_url(merge)),
        }
        for name, response in responses.items():
            self.assertEqual(response.status_code, expected, name)
        merge.refresh_from_db()
        self.assertEqual(merge.state, States.DRAFT)

    # --- Execute ------------------------------------------------------------

    def test_execute_requires_every_warning_to_be_acknowledged(self):
        models.PlanComponent.objects.filter(
            plan__offering=self.target, component__type="vcpu"
        ).update(price=Decimal(20))
        merge = self.previewed()
        warnings = codes(merge.preview["warnings"])
        self.assertIn("plan_price_difference", warnings)

        response = self.post(merge, "execute")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["missing_acknowledgements"], sorted(warnings))

        response = self.post(
            merge, "execute", data={"acknowledged_warnings": ["something_else"]}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["missing_acknowledgements"], sorted(warnings))
        merge.refresh_from_db()
        self.assertEqual(merge.state, States.PREVIEWED)

        response = self.post(
            merge, "execute", data={"acknowledged_warnings": sorted(warnings)}
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        merge.refresh_from_db()
        self.assertEqual(merge.state, States.DONE, merge.error_message)

    def test_execute_with_blockers_is_refused(self):
        self.plan_mapping.pop(self.b_plans["premium"])
        merge = self.previewed()
        self.assertIn("unmapped_plan", codes(merge.preview["blockers"]))

        response = self.post(merge, "execute")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("unmapped_plan", codes(response.data["blockers"]))
        merge.refresh_from_db()
        self.assertEqual(merge.state, States.PREVIEWED)

    def test_execute_requires_a_previewed_merge(self):
        merge = self.make_merge()
        response = self.post(merge, "execute")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_execute_queues_a_task_that_reaches_done(self):
        merge = self.previewed()

        response = self.post(merge, "execute")

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        merge.refresh_from_db()
        self.assertEqual(merge.state, States.DONE, merge.error_message)
        self.assertTrue(merge.verification["passed"])
        self.assertEqual(merge.verification["stage"], "execute")
        self.assertEqual(merge.progress["step"], "done")
        self.assertEqual(merge.progress["steps_done"], merge.progress["steps_total"])
        self.assertEqual(merge.progress["rows_done"], merge.progress["rows_total"])
        self.resource_a.refresh_from_db()
        self.assertEqual(self.resource_a.offering, self.target)

        self.client.force_authenticate(self.support)
        data = self.client.get(merge_url(merge)).data
        self.assertTrue(data["verification"]["execute"]["passed"])
        self.assertEqual(
            {check["code"] for check in data["verification"]["execute"]["checks"]},
            {check["code"] for check in merge.verification["execute"]["checks"]},
        )

    def test_execute_queues_the_merge_before_the_task_runs(self):
        merge = self.previewed()
        with mock.patch.object(tasks.execute_offering_merge, "delay") as delay:
            response = self.post(merge, "execute")

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["state"], States.QUEUED)
        delay.assert_called_once_with(merge.uuid.hex)

    def test_second_execute_while_queued_or_running_conflicts(self):
        merge = self.previewed()
        with mock.patch.object(tasks.execute_offering_merge, "delay") as delay:
            response = self.post(merge, "execute")
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

            response = self.post(merge, "execute")
            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

            models.OfferingMerge.objects.filter(pk=merge.pk).update(
                state=States.RUNNING
            )
            response = self.post(merge, "execute")
            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        delay.assert_called_once()

        # The queued task still runs the merge exactly once.
        models.OfferingMerge.objects.filter(pk=merge.pk).update(state=States.QUEUED)
        tasks.execute_offering_merge(merge.uuid.hex)
        merge.refresh_from_db()
        self.assertEqual(merge.state, States.DONE, merge.error_message)

    def test_a_task_delivered_twice_does_not_run_twice(self):
        merge = self.done()
        changes = merge.changes.count()

        # A redelivered task finds the merge done and is refused cleanly.
        tasks.execute_offering_merge(merge.uuid.hex)

        merge.refresh_from_db()
        self.assertEqual(merge.state, States.DONE)
        self.assertEqual(merge.changes.count(), changes)

    def test_progress_is_reported_between_registry_steps(self):
        merge = self.previewed()
        reports = []
        with mock.patch.object(
            offering_merge,
            "_write_progress",
            side_effect=lambda _connection, _pk, progress: reports.append(progress),
        ):
            self.post(merge, "execute")

        steps = [report["step"] for report in reports]
        self.assertIn("marketplace.Resource.offering", steps)
        self.assertEqual(
            steps[-5:],
            [
                "invoice_snapshots",
                "archive_sources",
                "recompute_summaries",
                "recalculate_counters",
                "verification",
            ],
        )
        done = [report["steps_done"] for report in reports]
        self.assertEqual(done, list(range(len(reports))))
        rows = [report["rows_done"] for report in reports]
        self.assertEqual(rows, sorted(rows))
        self.assertEqual(rows[-1], reports[-1]["rows_total"])
        self.assertGreater(rows[-1], 0)

    def test_failed_execution_is_stored_and_logged(self):
        merge = self.previewed()
        # A resource created after the preview changes what the merge would do.
        factories.ResourceFactory(offering=self.source_a, plan=self.a_plans["basic"])

        response = self.post(merge, "execute")

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        merge.refresh_from_db()
        self.assertEqual(merge.state, States.FAILED)
        self.assertIn("changed since it was previewed", merge.error_message)
        event = events(EventType.MARKETPLACE_OFFERING_MERGE_FAILED).get()
        self.assertEqual(event.context["operation"], "execution")
        # A failed merge may be previewed again.
        response = self.post(merge, "preview")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # --- Undo ---------------------------------------------------------------

    def test_undo_reaches_undone(self):
        merge = self.done()

        response = self.post(merge, "undo")

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        merge.refresh_from_db()
        self.assertEqual(merge.state, States.UNDONE)
        self.assertEqual(merge.verification["stage"], "undo")
        self.resource_a.refresh_from_db()
        self.assertEqual(self.resource_a.offering, self.source_a)
        self.source_a.refresh_from_db()
        self.assertEqual(self.source_a.state, OfferingStates.ACTIVE)

    def test_undo_moves_the_merge_to_undoing_and_refuses_a_second_request(self):
        merge = self.done()
        with mock.patch.object(tasks.undo_offering_merge, "delay") as delay:
            response = self.post(merge, "undo")
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
            self.assertEqual(response.data["state"], States.UNDOING)
            response = self.post(merge, "undo")
            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        delay.assert_called_once_with(merge.uuid.hex)

    def test_undo_requires_a_done_merge(self):
        for merge in (self.make_merge(), self.previewed()):
            response = self.post(merge, "undo")
            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_undo_refused_by_the_engine_answers_400_with_the_reason(self):
        merge = self.done()
        models.Resource.objects.filter(pk=self.resource_a.pk).update(
            plan=self.target_plans["Tier 2"]
        )

        response = self.post(merge, "undo")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("resource_changed", codes(response.data["blockers"]))
        merge.refresh_from_db()
        self.assertEqual(merge.state, States.DONE)

    def test_undo_refused_in_the_task_returns_to_done_with_the_reason(self):
        merge = self.done()
        models.Resource.objects.filter(pk=self.resource_a.pk).update(
            plan=self.target_plans["Tier 2"]
        )

        # The change lands between the API's check and the task.
        with mock.patch.object(offering_merge, "undo_blockers", return_value=[]):
            response = self.post(merge, "undo")

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        merge.refresh_from_db()
        self.assertEqual(merge.state, States.DONE)
        self.assertIn("resource_changed", merge.error_message)
        event = events(EventType.MARKETPLACE_OFFERING_MERGE_FAILED).get()
        self.assertEqual(event.context["operation"], "undo")

    # --- Events -------------------------------------------------------------

    def test_each_transition_is_logged_with_offerings_and_counts(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(merge_url(), self.payload(), format="json")
        merge = models.OfferingMerge.objects.get(uuid=response.data["uuid"])
        self.post(merge, "preview")
        self.post(merge, "execute")
        self.post(merge, "undo")

        for event_type in (
            EventType.MARKETPLACE_OFFERING_MERGE_CREATED,
            EventType.MARKETPLACE_OFFERING_MERGE_EXECUTED,
            EventType.MARKETPLACE_OFFERING_MERGE_UNDONE,
        ):
            with self.subTest(event_type=event_type):
                event = events(event_type).get()
                self.assertEqual(event.context["merge_uuid"], merge.uuid.hex)
                self.assertEqual(event.context["target_uuid"], self.target.uuid.hex)
                self.assertEqual(
                    sorted(event.context["source_uuids"]),
                    sorted([self.source_a.uuid.hex, self.source_b.uuid.hex]),
                )
        executed = events(EventType.MARKETPLACE_OFFERING_MERGE_EXECUTED).get()
        self.assertEqual(executed.context["counts"]["marketplace.Resource.offering"], 2)
        self.assertTrue(
            logging_models.Feed.objects.filter(
                event=executed, object_id=self.source_a.id
            ).exists()
        )
        self.assertFalse(
            events(EventType.MARKETPLACE_OFFERING_MERGE_VERIFICATION_FAILED).exists()
        )

    def test_failed_verification_is_logged(self):
        merge = self.previewed()
        report = {
            "passed": False,
            "checked_at": timezone.now().isoformat(),
            "checks": [{"code": "resource_counts", "passed": False, "details": {}}],
        }
        with mock.patch.object(
            offering_merge._Verification, "verify", return_value=report
        ):
            self.post(merge, "execute")

        merge.refresh_from_db()
        self.assertEqual(merge.state, States.DONE)
        event = events(EventType.MARKETPLACE_OFFERING_MERGE_VERIFICATION_FAILED).get()
        self.assertEqual(event.context["stage"], "execute")
        self.assertEqual(event.context["failed_checks"], "resource_counts")

    # --- Filters ------------------------------------------------------------

    def test_filters(self):
        first = self.make_merge(created_by=self.staff)
        other_target, _, _ = make_offering()
        other_source, _, _ = make_offering()
        second = make_merge(other_target, [other_source], {}, {})
        models.OfferingMerge.objects.filter(pk=second.pk).update(
            state=States.DONE, created=timezone.now() - datetime.timedelta(days=10)
        )
        self.client.force_authenticate(self.support)

        def found(**params):
            response = self.client.get(merge_url(), params)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            return {item["uuid"] for item in response.data}

        both = {first.uuid.hex, second.uuid.hex}
        self.assertEqual(found(), both)
        self.assertEqual(found(state=States.DRAFT), {first.uuid.hex})
        self.assertEqual(found(state=[States.DRAFT, States.DONE]), both)
        self.assertEqual(
            found(source_offering_uuid=self.source_b.uuid.hex), {first.uuid.hex}
        )
        self.assertEqual(
            found(target_offering_uuid=other_target.uuid.hex), {second.uuid.hex}
        )
        self.assertEqual(found(offering_uuid=other_source.uuid.hex), {second.uuid.hex})
        self.assertEqual(found(offering_uuid=self.target.uuid.hex), {first.uuid.hex})
        self.assertEqual(found(created_by_uuid=self.staff.uuid.hex), {first.uuid.hex})
        since = (timezone.now() - datetime.timedelta(days=1)).isoformat()
        self.assertEqual(found(created=since), {first.uuid.hex})
        self.assertEqual(found(created_before=since), {second.uuid.hex})


class OfferingMergeSuggestMappingTest(test.APITestCase):
    def setUp(self):
        self.target, self.target_plans, self.target_components = make_offering(
            components=(("cpu", BillingTypes.USAGE), ("ram", BillingTypes.USAGE)),
            plans=("Small", "Large"),
        )
        self.source, self.source_plans, _ = make_offering(
            components=(
                ("cpu", BillingTypes.USAGE),
                ("mem", BillingTypes.USAGE),
                ("gpu", BillingTypes.USAGE),
            ),
            plans=(" small ", "Huge"),
        )
        models.OfferingComponent.objects.filter(
            offering=self.source, type="mem"
        ).update(name="RAM")
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))

    def suggest(self, **params):
        return self.client.get(merge_url(action="suggest_mapping"), params)

    def test_plans_match_by_name_and_components_by_type_or_name(self):
        response = self.suggest(
            sources=self.source.uuid.hex, target=self.target.uuid.hex
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["plan_mapping"],
            {
                self.source_plans[" small "].uuid.hex: self.target_plans[
                    "Small"
                ].uuid.hex
            },
        )
        self.assertEqual(
            response.data["component_mapping"],
            {self.source.uuid.hex: {"cpu": "cpu", "mem": "ram"}},
        )
        self.assertEqual(
            [plan["name"] for plan in response.data["unmatched_plans"]], ["Huge"]
        )
        self.assertEqual(
            [item["type"] for item in response.data["unmatched_components"]], ["gpu"]
        )

    def test_sources_may_be_repeated_or_comma_separated(self):
        other, _, _ = make_offering(plans=("Large",))
        for params in (
            f"sources={self.source.uuid.hex},{other.uuid.hex}",
            f"sources={self.source.uuid.hex}&sources={other.uuid.hex}",
        ):
            response = self.client.get(
                f"{merge_url(action='suggest_mapping')}?{params}"
                f"&target={self.target.uuid.hex}"
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(
                set(response.data["component_mapping"]),
                {self.source.uuid.hex, other.uuid.hex},
            )

    def test_unknown_offerings_are_refused(self):
        for params in (
            {"target": self.target.uuid.hex},
            {"sources": self.source.uuid.hex},
            {"sources": "0" * 32, "target": self.target.uuid.hex},
            {"sources": self.source.uuid.hex, "target": "0" * 32},
            {"sources": "nope", "target": self.target.uuid.hex},
        ):
            with self.subTest(params=params):
                response = self.suggest(**params)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class OfferingMergeProgressVisibilityTest(TransactionTestCase):
    """Progress is committed outside the merge's transaction."""

    def test_progress_survives_a_rollback_and_is_visible_meanwhile(self):
        target, _, _ = make_offering()
        source, _, _ = make_offering()
        merge = make_merge(target, [source], {}, {})
        progress = offering_merge._Progress(merge, [("step", 3)])

        class Rollback(Exception):
            pass

        try:
            with transaction.atomic():
                self.assertTrue(connection.in_atomic_block)
                progress.start("step")
                # Another session sees it while this transaction is open.
                other = connections.create_connection("default")
                try:
                    with other.cursor() as cursor:
                        cursor.execute(
                            "SELECT progress->>'step' FROM marketplace_offeringmerge "
                            "WHERE id = %s",
                            [merge.pk],
                        )
                        self.assertEqual(cursor.fetchone()[0], "step")
                finally:
                    other.close()
                raise Rollback
        except Rollback:
            pass
        finally:
            progress.close()

        merge.refresh_from_db()
        self.assertEqual(merge.progress["step"], "step")
        self.assertEqual(merge.progress["rows_total"], 3)
