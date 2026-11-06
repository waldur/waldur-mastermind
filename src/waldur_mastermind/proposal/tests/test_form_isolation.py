"""Evaluation and compliance forms belong to an organisation, or to the whole
portal: a call uses, and its managers see, only the shared forms and those of
the organisation running it."""

from rest_framework import status, test

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.enums import ChecklistTypes
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import call_transfer, models
from waldur_mastermind.proposal.enums import CallStates
from waldur_mastermind.proposal.tests import factories, fixtures

STEP_FORMS_URL = "/api/proposal-protected-calls/step_checklists/"


class FormIsolationMixin:
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call.state = CallStates.DRAFT
        self.call.save()
        self.other = fixtures.ProposalFixture()
        self.other_call = self.other.call

        self.shared = self.form("Shared review")
        self.own = self.form("Own review", self.fixture.customer)
        self.foreign = self.form("Foreign review", self.other.customer)

    def form(self, name, organization=None, kind=ChecklistTypes.WORKFLOW_STEP):
        return checklist_factories.ChecklistFactory(
            name=name, checklist_type=kind, organization=organization
        )


class StepFormPickerTest(FormIsolationMixin, test.APITestCase):
    def names(self, user, **params):
        self.client.force_authenticate(user)
        response = self.client.get(STEP_FORMS_URL, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {c["name"] for c in response.data}

    def test_call_manager_sees_shared_and_own_forms_only(self):
        for user in (self.fixture.call_manager, self.fixture.call_organizer_user):
            self.assertEqual(self.names(user), {"Shared review", "Own review"})

    def test_other_organisations_manager_does_not_see_this_ones_forms(self):
        self.assertEqual(
            self.names(self.other.call_manager), {"Shared review", "Foreign review"}
        )

    def test_staff_and_support_see_every_form(self):
        support = structure_factories.UserFactory(is_support=True)
        for user in (self.fixture.staff, support):
            self.assertEqual(
                self.names(user), {"Shared review", "Own review", "Foreign review"}
            )

    def test_narrowed_to_one_organisation(self):
        self.assertEqual(
            self.names(self.fixture.staff, customer_uuid=self.other.customer.uuid.hex),
            {"Shared review", "Foreign review"},
        )
        # Naming another organisation reveals nothing of it.
        self.assertEqual(
            self.names(
                self.fixture.call_manager,
                customer_uuid=self.other.customer.uuid.hex,
            ),
            {"Shared review"},
        )

    def test_malformed_organisation_is_refused(self):
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(STEP_FORMS_URL, {"customer_uuid": "nope"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class StepFormAttachTest(FormIsolationMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.step = self.call.workflow_steps.get(key="administrative_check")

    def attach(self, user, form):
        self.client.force_authenticate(user)
        return self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, self.step),
            {"checklist": form.uuid.hex},
        )

    def test_call_manager_attaches_shared_and_own_forms(self):
        for form in (self.shared, self.own):
            response = self.attach(self.fixture.call_manager, form)
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            self.step.refresh_from_db()
            self.assertEqual(self.step.checklist, form)

    def test_call_manager_cannot_attach_another_organisations_form(self):
        response = self.attach(self.fixture.call_manager, self.foreign)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("checklist", response.data)
        self.step.refresh_from_db()
        self.assertIsNone(self.step.checklist)

    def test_staff_cannot_attach_another_organisations_form_either(self):
        response = self.attach(self.fixture.staff, self.foreign)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("another organization", str(response.data["checklist"]))

    def test_new_step_cannot_take_another_organisations_form(self):
        self.client.force_authenticate(self.fixture.call_manager)
        url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        payload = {"name": "Extra review", "kind": "assessment"}
        response = self.client.post(
            url, {**payload, "checklist": self.foreign.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.post(url, {**payload, "checklist": self.own.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


class ComplianceFormTest(FormIsolationMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        kind = ChecklistTypes.PROPOSAL_COMPLIANCE
        self.shared_compliance = self.form("Shared compliance", kind=kind)
        self.own_compliance = self.form("Own compliance", self.fixture.customer, kind)
        self.foreign_compliance = self.form(
            "Foreign compliance", self.other.customer, kind
        )
        # A call no proposal was made to yet: its compliance form may change.
        self.call = self.fixture.new_call
        self.call.add_user(self.fixture.call_manager, CallRole.MANAGER)

    def set_compliance(self, user, form):
        self.client.force_authenticate(user)
        return self.client.patch(
            factories.CallFactory.get_protected_url(self.call),
            {"compliance_checklist": form.uuid.hex},
        )

    def test_call_manager_sets_shared_or_own_compliance_form(self):
        for form in (self.shared_compliance, self.own_compliance):
            response = self.set_compliance(self.fixture.call_manager, form)
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            self.call.refresh_from_db()
            self.assertEqual(self.call.compliance_checklist, form)

    def test_another_organisations_compliance_form_is_refused(self):
        for user in (self.fixture.call_manager, self.fixture.staff):
            response = self.set_compliance(user, self.foreign_compliance)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertIn("compliance_checklist", response.data)

    def test_available_compliance_forms_are_the_organisations(self):
        self.client.force_authenticate(self.fixture.call_organizer_user)
        response = self.client.get(
            factories.CallFactory.get_protected_list_url(
                "available_compliance_checklists"
            ),
            {"customer_uuid": self.fixture.customer.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            {c["name"] for c in response.data},
            {"Shared compliance", "Own compliance"},
        )


class ImportFormTest(FormIsolationMixin, test.APITestCase):
    """An imported call resolves forms only among those it may use."""

    def document(self, form_name, kind=ChecklistTypes.WORKFLOW_STEP):
        data, _ = call_transfer.export_call(self.call)
        data.pop("checklists", None)
        for step in data["workflow_steps"]:
            step["checklist"] = None
        step = next(
            s for s in data["workflow_steps"] if s["key"] == "administrative_check"
        )
        step["checklist"] = {"name": form_name, "checklist_type": kind}
        data["compliance_checklist"] = None
        return data

    def import_(self, data, user=None):
        self.client.force_authenticate(user or self.fixture.call_organizer_user)
        response = self.client.post(
            factories.CallFactory.get_protected_list_url("import_call"),
            {"manager": self.fixture.manager.uuid.hex, "call_data": data},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        call = models.Call.objects.get(uuid=response.data["call_uuid"])
        form = call.workflow_steps.get(key="administrative_check").checklist
        return call, form, response.data["warnings"]

    def test_binds_shared_and_own_forms_by_name(self):
        for form in (self.shared, self.own):
            _, bound, warnings = self.import_(self.document(form.name))
            self.assertEqual(bound, form)
            self.assertEqual(warnings, [])

    def test_does_not_bind_or_reveal_another_organisations_form(self):
        for user in (self.fixture.call_organizer_user, self.fixture.staff):
            _, bound, warnings = self.import_(self.document("Foreign review"), user)
            self.assertIsNone(bound)
            self.assertEqual(
                warnings,
                [
                    "Workflow step checklist 'Foreign review' was not found on "
                    "this portal, skipped."
                ],
            )

    def test_compliance_form_resolves_within_the_organisation(self):
        foreign = self.form(
            "Foreign compliance",
            self.other.customer,
            ChecklistTypes.PROPOSAL_COMPLIANCE,
        )
        data = self.document("Shared review")
        data["compliance_checklist"] = {
            "name": foreign.name,
            "checklist_type": ChecklistTypes.PROPOSAL_COMPLIANCE,
        }
        call, _, warnings = self.import_(data)
        self.assertIsNone(call.compliance_checklist)
        self.assertEqual(
            warnings,
            [
                "Compliance checklist 'Foreign compliance' was not found on this "
                "portal, skipped."
            ],
        )

    def defining(self, name):
        data = self.document(name)
        data["checklists"] = [
            {
                "name": name,
                "checklist_type": ChecklistTypes.WORKFLOW_STEP,
                "description": "",
                "questions": [],
            }
        ]
        return data

    def test_defined_form_belongs_to_the_calls_organisation(self):
        _, form, _ = self.import_(self.defining("Brand new form"))
        self.assertEqual(form.name, "Brand new form")
        self.assertEqual(form.organization, self.fixture.customer)
        # Not offered to another organisation's managers.
        self.client.force_authenticate(self.other.call_manager)
        response = self.client.get(STEP_FORMS_URL)
        self.assertNotIn("Brand new form", {c["name"] for c in response.data})

    def test_another_organisations_form_name_does_not_shape_the_copy(self):
        # The name is taken only in the other organisation: the copy keeps it,
        # so it tells nothing of that organisation's forms.
        _, form, _ = self.import_(self.defining("Foreign review"))
        self.assertEqual(form.name, "Foreign review")
        self.assertNotEqual(form, self.foreign)
        self.assertEqual(form.organization, self.fixture.customer)
        # A name the call can already use gains the call's name.
        call, form, _ = self.import_(self.defining("Own review"))
        self.assertEqual(form.name, f"Own review ({call.name})")
        self.assertEqual(
            checklist_models.Checklist.objects.filter(name="Foreign review").count(),
            2,
        )


class ChecklistAdminOrganisationTest(test.APITestCase):
    def test_staff_scopes_a_checklist_to_an_organisation(self):
        staff = structure_factories.UserFactory(is_staff=True)
        customer = structure_factories.CustomerFactory()
        self.client.force_authenticate(staff)
        response = self.client.post(
            "/api/checklists-admin/",
            {
                "name": "Org form",
                "checklist_type": ChecklistTypes.WORKFLOW_STEP,
                "customer": customer.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["customer_name"], customer.name)
        checklist = checklist_models.Checklist.objects.get(uuid=response.data["uuid"])
        self.assertEqual(checklist.organization, customer)

        response = self.client.get(
            "/api/checklists-admin/", {"customer_uuid": customer.uuid.hex}
        )
        self.assertEqual([c["name"] for c in response.data], ["Org form"])
        response = self.client.get("/api/checklists-admin/", {"is_shared": True})
        self.assertNotIn("Org form", [c["name"] for c in response.data])
