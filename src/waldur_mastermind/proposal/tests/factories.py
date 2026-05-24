import datetime

import factory
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from rest_framework.reverse import reverse

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.tests import (
    factories as checklist_factories,
)
from waldur_core.core.tests.types import BaseMetaFactory
from waldur_core.permissions import fixtures as permissions_fixtures
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    AssignmentSources,
    COIDetectionJobStates,
    COIDetectionJobTypes,
    COIDetectionMethods,
    COISeverityLevels,
    COIStatuses,
    COITypes,
    ExpertiseProficiencyLevels,
    RequestedOfferingStates,
    ReviewerAffiliationTypes,
    ReviewerPoolInvitationStatuses,
)


class CallManagingOrganisationFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.CallManagingOrganisation],
):
    class Meta:
        model = models.CallManagingOrganisation

    customer = factory.SubFactory(structure_factories.CustomerFactory)

    @classmethod
    def get_url(cls, manager=None, action=None):
        if manager is None:
            manager = CallManagingOrganisationFactory()
        url = "http://testserver" + reverse(
            "call-managing-organisation-detail",
            kwargs={"uuid": manager.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("call-managing-organisation-list")
        return url if action is None else url + action + "/"


class CallFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Call]
):
    class Meta:
        model = models.Call

    name = factory.Sequence(lambda n: "name-%s" % n)
    manager = factory.SubFactory(CallManagingOrganisationFactory)
    created_by = factory.SubFactory(structure_factories.UserFactory)

    @classmethod
    def get_public_url(cls, call=None, action=None):
        if call is None:
            call = CallFactory()
        url = "http://testserver" + reverse(
            "proposal-public-call-detail",
            kwargs={"uuid": call.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_public_list_url(cls, action=None):
        url = "http://testserver" + reverse("proposal-public-call-list")
        return url if action is None else url + action + "/"

    @classmethod
    def get_protected_url(cls, call=None, action=None):
        if call is None:
            call = CallFactory()
        url = "http://testserver" + reverse(
            "proposal-protected-call-detail",
            kwargs={"uuid": call.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_protected_list_url(cls, action=None):
        url = "http://testserver" + reverse("proposal-protected-call-list")
        return url if action is None else url + action + "/"


class RequestedOfferingFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.RequestedOffering],
):
    class Meta:
        model = models.RequestedOffering

    call = factory.SubFactory(CallFactory)
    created_by = factory.SubFactory(structure_factories.UserFactory)
    offering = factory.SubFactory(marketplace_factories.OfferingFactory)
    state = RequestedOfferingStates.ACCEPTED

    @classmethod
    def get_url(cls, call=None, requested_offering=None):
        if requested_offering is None:
            requested_offering = RequestedOfferingFactory()
        return (
            CallFactory.get_protected_url(call, action="offerings")
            + requested_offering.uuid.hex
            + "/"
        )

    @classmethod
    def get_list_url(cls, call):
        return CallFactory.get_protected_url(call, action="offerings")

    @classmethod
    def get_provider_list_url(cls):
        url = "http://testserver" + reverse("proposal-requested-offering-list")
        return url

    @classmethod
    def get_provider_url(cls, requested_offering=None, action=None):
        if requested_offering is None:
            requested_offering = RequestedOfferingFactory()
        url = "http://testserver" + reverse(
            "proposal-requested-offering-detail",
            kwargs={"uuid": requested_offering.uuid.hex},
        )
        return url if action is None else url + action + "/"


class CallResourceTemplateFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.CallResourceTemplate],
):
    class Meta:
        model = models.CallResourceTemplate

    call = factory.SubFactory(CallFactory)
    requested_offering = factory.SubFactory(RequestedOfferingFactory)
    name = factory.Sequence(lambda n: "Template-%s" % n)
    description = factory.Sequence(lambda n: "Template description %s" % n)
    attributes = factory.LazyAttribute(lambda _: {"cpu": 2, "ram": 4096})
    limits = factory.LazyAttribute(lambda _: {"storage": 100})
    is_required = False
    created_by = factory.SubFactory(structure_factories.UserFactory)

    @classmethod
    def get_url(cls, call=None, template=None, action=None):
        if template is None:
            template = CallResourceTemplateFactory()
        if call is None:
            call = template.call
        url = (
            CallFactory.get_protected_url(call, action="resource_templates")
            + template.uuid.hex
            + "/"
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, call):
        return CallFactory.get_protected_url(call, action="resource_templates")


class RoundFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Round]
):
    class Meta:
        model = models.Round

    call = factory.SubFactory(CallFactory)
    start_time = factory.LazyFunction(
        lambda: timezone.now() + datetime.timedelta(days=5)
    )
    cutoff_time = factory.LazyFunction(
        lambda: timezone.now() + datetime.timedelta(days=10)
    )

    @classmethod
    def get_url(cls, call=None, call_round=None, action=None):
        if call_round is None:
            call_round = RoundFactory()
        return (
            (
                CallFactory.get_protected_url(call, action="rounds")
                + call_round.uuid.hex
                + "/"
            )
            if not action
            else (
                CallFactory.get_protected_url(call, action="rounds")
                + call_round.uuid.hex
                + "/"
                + action
                + "/"
            )
        )

    @classmethod
    def get_list_url(cls, call):
        return CallFactory.get_protected_url(call, action="rounds")

    @classmethod
    def get_own_url(cls, round_obj=None, action=None):
        # Default get_url() returns round url through Call
        # Use this method to get round url directly
        if round_obj is None:
            round_obj = RoundFactory()
        url = "http://testserver" + reverse(
            "call-round-detail",
            kwargs={"uuid": round_obj.uuid.hex},
        )
        return url if action is None else url + action + "/"


class ProposalFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Proposal]
):
    class Meta:
        model = models.Proposal

    round = factory.SubFactory(RoundFactory)
    duration_in_days = 10
    created_by = factory.SubFactory(structure_factories.UserFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)

    @classmethod
    def get_url(cls, proposal=None, action=None):
        if proposal is None:
            proposal = ProposalFactory()
        url = "http://testserver" + reverse(
            "proposal-proposal-detail",
            kwargs={"uuid": proposal.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("proposal-proposal-list")
        return url if action is None else url + action + "/"


class RequestedResourceFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.RequestedResource],
):
    class Meta:
        model = models.RequestedResource

    proposal = factory.SubFactory(ProposalFactory)
    created_by = factory.SubFactory(structure_factories.UserFactory)
    resource = factory.SubFactory(marketplace_factories.ResourceFactory)
    requested_offering = factory.SubFactory(RequestedOfferingFactory)

    @classmethod
    def get_url(cls, proposal, requested_resource=None):
        if requested_resource is None:
            requested_resource = RequestedResourceFactory()
        return (
            ProposalFactory.get_url(proposal, action="resources")
            + requested_resource.uuid.hex
            + "/"
        )

    @classmethod
    def get_list_url(cls, proposal):
        return ProposalFactory.get_url(proposal, action="resources")

    @classmethod
    def get_provider_list_url(cls):
        url = "http://testserver" + reverse("proposal-requested-resource-list")
        return url

    @classmethod
    def get_provider_url(cls, requested_resource=None, action=None):
        if requested_resource is None:
            requested_resource = RequestedResourceFactory()
        url = "http://testserver" + reverse(
            "proposal-requested-resource-detail",
            kwargs={"uuid": requested_resource.uuid.hex},
        )
        return url if action is None else url + action + "/"


class ReviewFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Review]
):
    class Meta:
        model = models.Review

    proposal = factory.SubFactory(ProposalFactory)
    reviewer = factory.SubFactory(structure_factories.UserFactory)

    @classmethod
    def get_url(cls, review=None, action=None):
        if review is None:
            review = ReviewFactory()
        url = "http://testserver" + reverse(
            "proposal-review-detail",
            kwargs={"uuid": review.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("proposal-review-list")
        return url if action is None else url + action + "/"


class ProposalProjectRoleMappingFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ProposalProjectRoleMapping],
):
    class Meta:
        model = models.ProposalProjectRoleMapping

    call = factory.SubFactory(CallFactory)
    proposal_role = factory.LazyFunction(
        lambda: permissions_fixtures.ProposalRole.MANAGER
    )
    project_role = factory.LazyFunction(
        lambda: permissions_fixtures.ProjectRole.MANAGER
    )

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse(
            "call-proposal-project-role-mapping-list",
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_url(cls, mapping=None, action=None):
        if mapping is None:
            mapping = ProposalProjectRoleMappingFactory()
        url = "http://testserver" + reverse(
            "call-proposal-project-role-mapping-detail",
            kwargs={"uuid": mapping.uuid.hex},
        )

        return url if action is None else url + action + "/"


# Checklist Integration Factories


class ProposalChecklistCompletionFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[checklist_models.ChecklistCompletion],
):
    class Meta:
        model = checklist_models.ChecklistCompletion

    checklist = factory.SubFactory(checklist_factories.ChecklistFactory)
    scope_content_type = factory.LazyAttribute(
        lambda obj: ContentType.objects.get_for_model(models.Proposal)
    )
    scope_object_id = factory.SelfAttribute("proposal.id")

    # Helper field to create the proposal - not part of the model
    proposal = factory.SubFactory(ProposalFactory)

    @classmethod
    def get_url(cls, completion=None, action=None):
        if completion is None:
            completion = ProposalChecklistCompletionFactory()
        url = "http://testserver" + reverse(
            "proposal-checklist-completion-detail",
            kwargs={"uuid": completion.uuid.hex},
        )
        return url if action is None else url + action + "/"


# =============================================================================
# Reviewer Profile and COI Factories
# =============================================================================


class ReviewerProfileFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ReviewerProfile],
):
    class Meta:
        model = models.ReviewerProfile

    user = factory.SubFactory(structure_factories.UserFactory)
    orcid_id = factory.Sequence(lambda n: f"0000-0001-0000-{n:04d}")
    biography = factory.Faker("paragraph")
    alternative_names = factory.LazyFunction(list)

    @classmethod
    def get_url(cls, profile=None, action=None):
        if profile is None:
            profile = ReviewerProfileFactory()
        url = "http://testserver" + reverse(
            "reviewer-profile-detail",
            kwargs={"uuid": profile.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("reviewer-profile-list")
        return url if action is None else url + action + "/"


class ReviewerAffiliationFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ReviewerAffiliation],
):
    class Meta:
        model = models.ReviewerAffiliation

    reviewer_profile = factory.SubFactory(ReviewerProfileFactory)
    organization = factory.SubFactory(structure_factories.CustomerFactory)
    organization_name = factory.LazyAttribute(lambda obj: obj.organization.name)
    department = factory.Faker("word")
    position_title = factory.Faker("job")
    start_date = factory.Faker("date_object")
    end_date = None
    is_primary = True
    affiliation_type = ReviewerAffiliationTypes.EMPLOYMENT


class ExpertiseCategoryFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ExpertiseCategory],
):
    class Meta:
        model = models.ExpertiseCategory

    name = factory.Sequence(lambda n: f"Category {n}")
    code = factory.Sequence(lambda n: f"CAT{n:04d}")
    description = factory.Faker("sentence")
    parent = None
    level = 0


class ReviewerExpertiseFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ReviewerExpertise],
):
    class Meta:
        model = models.ReviewerExpertise

    reviewer_profile = factory.SubFactory(ReviewerProfileFactory)
    expertise_keyword = factory.Sequence(lambda n: f"expertise_{n}")
    expertise_category = None
    proficiency_level = ExpertiseProficiencyLevels.EXPERT
    years_experience = factory.Faker("random_int", min=1, max=30)


class ReviewerPublicationFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ReviewerPublication],
):
    class Meta:
        model = models.ReviewerPublication

    reviewer_profile = factory.SubFactory(ReviewerProfileFactory)
    title = factory.Faker("sentence", nb_words=10)
    doi = factory.Sequence(lambda n: f"10.1234/test.{n:06d}")
    publication_year = factory.Faker("random_int", min=2010, max=2025)
    venue = factory.Faker("company")
    venue_type = "journal"
    abstract = factory.Faker("paragraph")
    coauthors = factory.LazyFunction(list)
    external_ids = factory.LazyFunction(dict)
    is_excluded_from_matching = False


class ReviewerStatsFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ReviewerStats],
):
    class Meta:
        model = models.ReviewerStats

    reviewer_profile = factory.SubFactory(ReviewerProfileFactory)
    total_reviews_completed = factory.Faker("random_int", min=0, max=50)
    total_reviews_declined = factory.Faker("random_int", min=0, max=10)
    total_reviews_timeout = factory.Faker("random_int", min=0, max=5)
    average_review_time_days = factory.Faker(
        "pyfloat", min_value=1.0, max_value=30.0, right_digits=1
    )


class CallCOIConfigurationFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.CallCOIConfiguration],
):
    class Meta:
        model = models.CallCOIConfiguration

    call = factory.SubFactory(CallFactory)
    coauthorship_lookback_years = 3
    coauthorship_threshold_papers = 1
    institutional_lookback_years = 2
    include_same_department = True
    include_same_institution = True
    auto_detect_coauthorship = True
    auto_detect_institutional = True
    auto_detect_named_personnel = True


class ConflictOfInterestFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ConflictOfInterest],
):
    class Meta:
        model = models.ConflictOfInterest

    reviewer = factory.SubFactory(ReviewerProfileFactory)
    proposal = factory.SubFactory(ProposalFactory)
    call = factory.LazyAttribute(lambda obj: obj.proposal.round.call)
    coi_type = COITypes.INST_SAME
    severity = COISeverityLevels.REAL
    detection_method = COIDetectionMethods.AUTOMATED
    evidence_description = factory.Faker("sentence")
    evidence_data = factory.LazyFunction(dict)
    status = COIStatuses.PENDING

    @classmethod
    def get_url(cls, coi=None, action=None):
        if coi is None:
            coi = ConflictOfInterestFactory()
        url = "http://testserver" + reverse(
            "conflict-of-interest-detail",
            kwargs={"uuid": coi.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("conflict-of-interest-list")
        return url if action is None else url + action + "/"


class COIDisclosureFormFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.COIDisclosureForm],
):
    class Meta:
        model = models.COIDisclosureForm

    reviewer = factory.SubFactory(ReviewerProfileFactory)
    call = factory.SubFactory(CallFactory)
    certified = False
    certification_statement = (
        "I certify that I have disclosed all conflicts of interest."
    )
    has_financial_interests = False
    has_personal_relationships = False
    has_other_conflicts = False
    valid_until = factory.Faker("future_date")
    is_current = True


class CallReviewerPoolFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.CallReviewerPool],
):
    class Meta:
        model = models.CallReviewerPool

    call = factory.SubFactory(CallFactory)
    reviewer = factory.SubFactory(ReviewerProfileFactory)
    invitation_status = ReviewerPoolInvitationStatuses.ACCEPTED
    max_assignments = 5
    current_assignments = 0

    @classmethod
    def get_url(cls, pool_member=None, action=None):
        if pool_member is None:
            pool_member = CallReviewerPoolFactory()
        url = "http://testserver" + reverse(
            "call-reviewer-pool-detail",
            kwargs={"uuid": pool_member.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("call-reviewer-pool-list")
        return url if action is None else url + action + "/"


class COIDetectionJobFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.COIDetectionJob],
):
    class Meta:
        model = models.COIDetectionJob

    call = factory.SubFactory(CallFactory)
    job_type = COIDetectionJobTypes.FULL_CALL
    state = COIDetectionJobStates.PENDING
    total_pairs = 0
    processed_pairs = 0
    conflicts_found = 0


class MatchingConfigurationFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.MatchingConfiguration],
):
    class Meta:
        model = models.MatchingConfiguration

    call = factory.SubFactory(CallFactory)
    affinity_method = "combined"
    keyword_weight = 0.4
    text_weight = 0.6
    min_reviewers_per_proposal = 3
    max_reviewers_per_proposal = 5
    min_proposals_per_reviewer = 3
    max_proposals_per_reviewer = 10
    algorithm = "minmax"
    min_affinity_threshold = 0.1
    use_reviewer_bids = True
    bid_weight = 0.3


class AssignmentBatchFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.AssignmentBatch],
):
    class Meta:
        model = models.AssignmentBatch

    call = factory.SubFactory(CallFactory)
    reviewer_pool_entry = factory.SubFactory(CallReviewerPoolFactory)
    status = AssignmentBatchStatuses.DRAFT
    source = AssignmentSources.MANUAL
    created_by = factory.SubFactory(structure_factories.UserFactory)
    manager_notes = ""

    @classmethod
    def get_url(cls, batch=None, action=None):
        if batch is None:
            batch = AssignmentBatchFactory()
        url = "http://testserver" + reverse(
            "assignment-batch-detail",
            kwargs={"uuid": batch.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("assignment-batch-list")
        return url if action is None else url + action + "/"


class AssignmentItemFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.AssignmentItem],
):
    class Meta:
        model = models.AssignmentItem

    batch = factory.SubFactory(AssignmentBatchFactory)
    proposal = factory.SubFactory(ProposalFactory)
    status = AssignmentItemStatuses.PENDING
    affinity_score = 0.75
    has_coi = False

    @classmethod
    def get_url(cls, item=None, action=None):
        if item is None:
            item = AssignmentItemFactory()
        url = "http://testserver" + reverse(
            "assignment-item-detail",
            kwargs={"uuid": item.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("assignment-item-list")
        return url if action is None else url + action + "/"


# =============================================================================
# Workflow Step Factories
# =============================================================================


class CallWorkflowStepFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.CallWorkflowStep],
):
    class Meta:
        model = models.CallWorkflowStep

    call = factory.SubFactory(CallFactory)
    step = "administrative_check"
    is_enabled = True

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        # Mandatory steps (e.g. allocation_decision) are pre-seeded on Call
        # creation; reuse the existing row instead of triggering unique_together.
        call = kwargs.pop("call")
        step = kwargs.pop("step")
        instance, _ = model_class.objects.update_or_create(
            call=call, step=step, defaults=kwargs
        )
        return instance

    @classmethod
    def get_list_url(cls, call):
        return CallFactory.get_protected_url(call, action="workflow_steps")

    @classmethod
    def get_url(cls, call=None, workflow_step=None):
        if workflow_step is None:
            workflow_step = CallWorkflowStepFactory()
        return (
            CallFactory.get_protected_url(call, action="workflow_steps")
            + workflow_step.uuid.hex
            + "/"
        )
