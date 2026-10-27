from django.contrib import admin
from django.forms.models import model_to_dict
from django.test import RequestFactory
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from waldur_core.checklist.enums import ChecklistTypes
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import ResourceTemplateSelectionModes
from waldur_mastermind.proposal.tests import factories as proposal_factories


class CallModelValidationTest(APITestCase):
    """Call.clean() on a call that is already saved."""

    def setUp(self):
        self.call = proposal_factories.CallFactory()
        # A fresh instance, so the tracker compares against what is stored.
        self.call = models.Call.objects.get(pk=self.call.pk)

    def add_proposal(self):
        proposal_factories.ProposalFactory(
            round=proposal_factories.RoundFactory(call=self.call)
        )

    def new_checklist(self):
        return checklist_factories.ChecklistFactory(
            checklist_type=ChecklistTypes.PROPOSAL_COMPLIANCE
        )

    def test_saved_call_without_proposals_validates(self):
        self.call.full_clean()

    def test_switching_saved_call_to_single_selection_validates(self):
        self.call.resource_template_selection_mode = (
            ResourceTemplateSelectionModes.SINGLE
        )
        self.call.full_clean()

    def test_saved_call_with_proposals_validates_when_nothing_locked_changed(self):
        self.add_proposal()
        self.call.name = "Renamed"
        self.call.full_clean()

    def test_checklist_change_refused_when_proposals_exist(self):
        self.add_proposal()
        self.call.compliance_checklist = self.new_checklist()

        with self.assertRaisesMessage(
            ValidationError, "Cannot change compliance checklist when proposals exist"
        ):
            self.call.clean()

    def test_checklist_change_allowed_without_proposals(self):
        self.call.compliance_checklist = self.new_checklist()
        self.call.clean()

    def test_slug_template_change_refused_when_proposals_exist(self):
        self.add_proposal()
        self.call.proposal_slug_template = "{call_slug}-{counter_padded}"

        with self.assertRaisesMessage(
            ValidationError, "Cannot change proposal slug template when proposals exist"
        ):
            self.call.clean()

    def test_slug_template_change_allowed_without_proposals(self):
        self.call.proposal_slug_template = "{call_slug}-{counter_padded}"
        self.call.clean()

    def test_blank_and_missing_slug_template_are_the_same(self):
        self.add_proposal()
        for stored, new in ((None, ""), ("", None)):
            with self.subTest(stored=stored, new=new):
                self.call.proposal_slug_template = stored
                self.call.save()
                self.call = models.Call.objects.get(pk=self.call.pk)
                self.call.proposal_slug_template = new
                self.call.clean()


class CallAdminFormTest(APITestCase):
    def test_change_form_validates_saved_call(self):
        # The admin form validates the whole model, Call.clean() included.
        call = proposal_factories.CallFactory()
        proposal_factories.ProposalFactory(
            round=proposal_factories.RoundFactory(call=call)
        )
        request = RequestFactory().get("/")
        request.user = structure_factories.UserFactory(is_staff=True, is_superuser=True)
        form_class = admin.site._registry[models.Call].get_form(
            request, call, change=True
        )
        initial = model_to_dict(call, fields=form_class.base_fields)
        data = {key: value for key, value in initial.items() if value is not None}

        form = form_class(data=data, instance=call)

        self.assertTrue(form.is_valid(), form.errors)
