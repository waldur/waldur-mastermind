"""No AI assistant tool tells a viewer what the proposal API hides from them.

One proposal world is seeded with unique values ("canaries") wherever the
proposal API restricts who reads what: the applicant's identity under a call
that conceals it, each review's author, comments and score, each provider's
part of the request, and the proposal itself. Every registered tool is then
called as each viewer who must not read some of them, and the whole result --
``summary`` text included -- is searched for the canaries.

A tool registered without probe arguments here, or an explicit exemption,
fails ``test_every_tool_is_probed_or_exempt``: a new tool cannot skip the
check by accident. A canary no tool emits even to staff proves nothing, so
``test_staff_reads_every_canary`` keeps the seeded set honest.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.utils import timezone

from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    ReviewerPoolInvitationStatuses,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_provider_scope import (
    TwoProviderProposalBase,
)

# Tools that read nothing of the proposal domain, so a probe would only
# exercise their own backends.
EXEMPT_TOOLS = {
    ToolName.ASK_USER: "asks the user a question; reads no data",
    ToolName.SEARCH_TOOLS: "returns tool schemas; reads no data",
    ToolName.SEARCH_DOCS: "searches the portal documentation; reads no data",
    ToolName.CREATE_VM: "provisions a VM; reads no proposal data",
    ToolName.PLAN_VM: "plans a VM order; reads no proposal data",
    ToolName.LIST_HPC_ALLOCATIONS: "reads the user's own HPC allocations",
    ToolName.GET_HPC_CLUSTER_STATUS: "reads cluster state from the site agent",
    ToolName.FIND_HPC_SOFTWARE: "reads cluster software from the site agent",
    ToolName.PLAN_HPC_JOB: "plans a batch job; reads no proposal data",
    ToolName.SUBMIT_HPC_JOB: "submits a batch job; reads no proposal data",
    ToolName.LIST_HPC_JOBS: "reads the user's own jobs from the site agent",
    ToolName.GET_HPC_JOB: "reads one of the user's jobs from the site agent",
    ToolName.CANCEL_HPC_JOB: "cancels one of the user's jobs",
    ToolName.GET_HPC_ALLOCATION_USAGE: "reads allocation usage from the agent",
    ToolName.GET_HPC_JOB_STATS: "reads the user's job statistics",
    ToolName.GET_HPC_CLUSTER_LOAD: "reads cluster load from the site agent",
    ToolName.PLOT_HPC_OUTPUT: "plots a file from the user's job",
    # One marketplace record by UUID: the proposal world holds no order or
    # resource, and who reads one is the marketplace's rule, not the call's.
    ToolName.GET_ORDER: "reads one marketplace order",
    ToolName.GET_RESOURCE: "reads one marketplace resource",
    ToolName.GET_RESOURCE_USAGE: "reads one marketplace resource's usage",
    ToolName.EXPLAIN_RESOURCE_PAUSED_REASON: "reads one marketplace resource",
}

OWN_SCORE = 3
PEER_SCORE = 10
AVERAGE_SCORE = 6.5  # of OWN_SCORE and PEER_SCORE
SCORE_KEY = re.compile(r"score|avg|average|mean", re.IGNORECASE)
AVERAGE_IN_TEXT = re.compile(r"(?<![\d.])6\.5(?!\d)")


@dataclass
class Viewer:
    user: object
    canaries: set[str] = field(default_factory=set)
    scores: set[float] = field(default_factory=set)


class ChatToolLeakCanaryTest(TwoProviderProposalBase):
    def setUp(self):
        super().setUp()
        self.applicant_canaries = self._plant_applicant()
        self.proposal_canaries = self._plant_proposal()
        self.request_a_canaries = self._plant_request(self.resource_a, "a")
        self.request_b_canaries = self._plant_request(self.resource_b, "b")
        self.own_reviewer, own_review_canaries = self._plant_review("own", OWN_SCORE)
        self.call.add_user(self.own_reviewer, CallRole.REVIEWER)
        self.peer_reviewer, peer_review_canaries = self._plant_review(
            "peer", PEER_SCORE
        )
        self.call.add_user(self.peer_reviewer, CallRole.REVIEWER)
        self.review_canaries = own_review_canaries | peer_review_canaries
        self.revoked_reviewer = self._revoked_reviewer()
        self.outsider = structure_factories.UserFactory()

        everything = (
            self.applicant_canaries
            | self.proposal_canaries
            | self.request_a_canaries
            | self.request_b_canaries
            | self.review_canaries
        )
        all_scores = {OWN_SCORE, PEER_SCORE, AVERAGE_SCORE}
        self.viewers = {
            # Reads the proposal and its own review; the applicant is
            # concealed and the other reviews, scores included, are not theirs.
            "reviewer": Viewer(
                self.own_reviewer,
                self.applicant_canaries | peer_review_canaries,
                {PEER_SCORE, AVERAGE_SCORE},
            ),
            # An evaluator reading no review (managers.filter_reviews_for_user).
            "panel member": Viewer(
                self.panel_member,
                self.applicant_canaries | self.review_canaries,
                all_scores,
            ),
            # Reads only its own offering's part, no review
            # (permissions.offering_scope).
            "provider A": Viewer(
                self.manager_a,
                self.applicant_canaries
                | self.request_b_canaries
                | self.review_canaries,
                all_scores,
            ),
            "provider B": Viewer(
                self.manager_b,
                self.applicant_canaries
                | self.request_a_canaries
                | self.review_canaries,
                all_scores,
            ),
            # Its assignment closed with the role: it reads nothing any more.
            "revoked reviewer": Viewer(
                self.revoked_reviewer,
                everything,
                all_scores,
            ),
            # Reads no review before the decision.
            "applicant": Viewer(
                self.applicant,
                self.review_canaries,
                all_scores,
            ),
            "outsider": Viewer(self.outsider, everything, all_scores),
        }
        self.everything = everything

    # The world

    def _plant_applicant(self):
        applicant = self.applicant
        applicant.full_name = "Zqapplicant Fullname"
        applicant.username = "zqapplicant-username"
        applicant.email = "zqapplicant@example.org"
        applicant.organization = "Zqapplicant Organisation"
        applicant.save()
        models.CallApplicantVisibilityConfig.objects.update_or_create(
            call=self.call,
            defaults={
                f"expose_{name}": False
                for name in models.CallApplicantVisibilityConfig.get_attribute_names()
            },
        )
        return {applicant.full_name, applicant.email, applicant.organization}

    def _plant_proposal(self):
        self.proposal.name = "Zqproposal Title"
        self.proposal.project_summary = "Zqproposal project summary"
        self.proposal.save()
        return {self.proposal.name, self.proposal.project_summary}

    def _plant_request(self, resource, letter):
        resource.limits = {f"zqlimit{letter}": 100}
        resource.save()
        return {f"zqlimit{letter}"}

    def _plant_review(self, who, score):
        reviewer = structure_factories.UserFactory(
            full_name=f"Zq{who}reviewer Fullname",
            email=f"zq{who}reviewer@example.org",
        )
        factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=reviewer,
            state=models.Review.States.SUBMITTED,
            summary_score=score,
        )
        return reviewer, {reviewer.full_name, reviewer.email}

    def _revoked_reviewer(self):
        """A reviewer sent an assignment to the proposal, whose call role was
        revoked before they answered it."""
        user = structure_factories.UserFactory()
        profile = factories.ReviewerProfileFactory(user=user)
        entry = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=profile,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        )
        batch = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=entry,
            status=AssignmentBatchStatuses.SENT,
            expires_at=timezone.now() + timedelta(days=7),
        )
        factories.AssignmentItemFactory(
            batch=batch, proposal=self.proposal, status=AssignmentItemStatuses.PENDING
        )
        # Accepting the pool invitation gave the call's reviewer role.
        self.call.remove_user(user, CallRole.REVIEWER, removed_by=self.staff)
        return user

    # Probes

    def probes(self):
        """Arguments each tool is called with: every way a caller can point
        it at the seeded proposal, its call, its people and its project."""
        proposal = {
            "uuid": self.proposal.uuid.hex,
            "slug": self.proposal.slug,
            "name": self.proposal.name,
        }
        call = {"uuid": self.call.uuid.hex, "name": self.call.name}
        project = self.proposal.project
        project_args = [
            {"project_uuid": project.uuid.hex},
            {"project_name": project.name},
        ]
        customer = self.call.manager.customer
        customer_args = [
            {"customer_uuid": customer.uuid.hex},
            {"customer_name": customer.name},
        ]
        people = [
            self.applicant,
            self.own_reviewer,
            self.peer_reviewer,
            self.manager_a,
            self.manager_b,
        ]
        return {
            ToolName.PROPOSAL_OVERVIEW: [
                {key: value} for key, value in proposal.items()
            ],
            ToolName.REVIEW_ASSISTANT: [
                {key: value} for key, value in proposal.items()
            ],
            ToolName.LIST_PROPOSALS: [
                {},
                {"mine": True},
                {"call_uuid": call["uuid"]},
                {"call_name": call["name"]},
                {"search": self.proposal.name},
            ],
            ToolName.CALL_INSIGHTS: [{key: value} for key, value in call.items()],
            ToolName.GUIDE_PROPOSAL: [{key: value} for key, value in call.items()],
            ToolName.LIST_CALLS: [{}, {"search": self.call.name}],
            ToolName.FIND_MATCHING_CALLS: [{"keywords": self.proposal.name.split()}],
            ToolName.REVIEW_WORKLOAD: [{}],
            ToolName.GET_USER_OVERVIEW: [{"user_email": user.email} for user in people],
            ToolName.LIST_ORGANIZATIONS: [{}, {"search": customer.name}],
            ToolName.LIST_PROJECTS: [{}],
            ToolName.GET_PROJECT: project_args,
            ToolName.GET_PROJECT_QUOTA: project_args,
            ToolName.GET_PROJECT_RESOURCES: project_args,
            ToolName.EXPLAIN_PROJECT_CREDIT_BALANCE: project_args,
            ToolName.EXPLAIN_INVOICE_COMPENSATIONS: project_args,
            ToolName.DISPLAY_USER_RESOURCES: [{}, *project_args],
            ToolName.LIST_ORDERS: [{}, *project_args],
            ToolName.LIST_INVOICES: [{}, *customer_args],
            ToolName.GET_CUSTOMER_CREDIT_OVERVIEW: [{}, *customer_args],
            ToolName.CHART_COSTS: [*customer_args, *project_args],
            ToolName.LIST_OVERDRAWN_PROJECTS: [{}],
            ToolName.SEARCH_OFFERINGS: [
                {"keyword": offering.name}
                for offering in (self.offering_a, self.offering_b)
            ],
            ToolName.GET_OFFERING: [
                {"uuid": offering.uuid.hex}
                for offering in (self.offering_a, self.offering_b)
            ],
            ToolName.COMPARE_OFFERINGS: [
                {"uuids": [self.offering_a.uuid.hex, self.offering_b.uuid.hex]}
            ],
            ToolName.LIST_CATEGORIES: [{}],
        }

    def run_probes(self, user):
        """Every probe's result for ``user``, as (tool, arguments, result)."""
        for name, argument_sets in self.probes().items():
            tool = tool_registry._tools[name]
            for arguments in argument_sets:
                try:
                    result = tool.execute(user, dict(arguments))
                except PermissionDenied:
                    # What ToolExecutor turns it into.
                    result = {"type": "error", "error": "Permission denied"}
                yield name, arguments, result

    # Detection

    @staticmethod
    def _text(result) -> str:
        return json.dumps(result, default=str).lower()

    @classmethod
    def _scores(cls, value, path=()):
        """The numbers in ``value`` held under a score-like key."""
        if isinstance(value, dict):
            for key, item in value.items():
                yield from cls._scores(item, (*path, str(key)))
        elif isinstance(value, list | tuple):
            for item in value:
                yield from cls._scores(item, path)
        elif isinstance(value, int | float) and not isinstance(value, bool):
            if any(SCORE_KEY.search(key) for key in path):
                yield ".".join(path), value

    def leaks(self, viewer: Viewer, arguments: dict, result) -> list[str]:
        text = self._text(result)
        # A value the caller passed in, echoed back ("Proposal '<name>' not
        # found"), tells them nothing.
        for value in arguments.values():
            if isinstance(value, str):
                text = text.replace(value.lower(), "")
        found = [
            f"canary {canary!r}"
            for canary in sorted(viewer.canaries)
            if canary.lower() in text
        ]
        found += [
            f"score {value} at {path}"
            for path, value in self._scores(result)
            if value in viewer.scores
        ]
        if AVERAGE_SCORE in viewer.scores and AVERAGE_IN_TEXT.search(text):
            found.append(f"average score {AVERAGE_SCORE} in text")
        return found

    # Tests

    def test_every_tool_is_probed_or_exempt(self):
        registered = set(tool_registry._tools)
        probes = self.probes()
        probed = set(probes)
        self.assertEqual(
            {name for name, argument_sets in probes.items() if not argument_sets},
            set(),
            "a probed tool must be called at least once",
        )
        self.assertFalse(probed & set(EXEMPT_TOOLS), "a tool is both probed and exempt")
        self.assertEqual(
            registered - probed - set(EXEMPT_TOOLS),
            set(),
            "Add probe arguments for these tools to probes() (or, if they read "
            "no proposal data, an exemption with a reason to EXEMPT_TOOLS).",
        )
        self.assertEqual(
            (probed | set(EXEMPT_TOOLS)) - registered, set(), "stale entries"
        )

    def test_staff_reads_every_canary(self):
        text = "".join(self._text(result) for *_, result in self.run_probes(self.staff))
        self.assertEqual(
            {canary for canary in self.everything if canary.lower() not in text},
            set(),
            "No tool emits these canaries even to staff, so the test cannot "
            "tell whether they leak.",
        )

    def test_no_viewer_reads_what_the_proposal_api_hides_from_them(self):
        found = []
        for label, viewer in self.viewers.items():
            for name, arguments, result in self.run_probes(viewer.user):
                for leak in self.leaks(viewer, arguments, result):
                    found.append(f"{name.value}({arguments}) to {label}: {leak}")
        self.assertEqual(found, [], "\n" + "\n".join(found))
