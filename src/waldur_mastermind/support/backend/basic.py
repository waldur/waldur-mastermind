import logging
from datetime import timedelta
from functools import cached_property

from constance import config
from django.core.cache import cache
from django.utils import timezone

from waldur_mastermind.support import models

from . import SupportBackend, SupportBackendError

logger = logging.getLogger(__name__)


class BasicBackend(SupportBackend):
    backend_name = "basic"

    #: Status a new ticket opens in. `IssueStatus` cannot supply this: it is a
    #: registry of *terminal* statuses only — its `type` has just RESOLVED and
    #: CANCELED — so the non-terminal status a ticket starts in is never a row
    #: in that table.
    default_status = "Open"

    @classmethod
    def from_settings(cls, settings_dict=None):
        """Create a BasicBackend instance, optionally configured from a settings dict."""
        return cls()

    def create_issue(self, issue):
        issue.backend_id = f"WLD-{issue.uuid.hex[:8].upper()}"
        issue.key = issue.backend_id

        if not issue.status:
            issue.status = self.default_status

        if config.WALDUR_SUPPORT_SLA_ENABLED:
            self._set_sla_deadlines(issue)

        if config.WALDUR_SUPPORT_AUTO_ASSIGN and not issue.assignee:
            self._auto_assign(issue)

        issue.save()

    def update_issue(self, issue):
        if issue.tracker.has_changed("status"):
            old_status = issue.tracker.previous("status")
            new_status = issue.status
            if not models.IssueStatusTransition.is_transition_allowed(
                old_status, new_status
            ):
                raise SupportBackendError(
                    f"Status transition from '{old_status}' to '{new_status}' is not allowed."
                )

            # Set the resolution date when the issue closes, and clear it again
            # when it is reopened. Assigning `now()` unconditionally would also
            # rewrite the closure timestamp when a ticket moves between two
            # terminal statuses, skewing the "closed this month" count.
            issue.sync_resolution_date()

        issue.save()

    def delete_issue(self, issue):
        return

    def create_comment(self, comment):
        comment.backend_id = f"WLD-C-{comment.uuid.hex[:8].upper()}"
        comment.save(update_fields=["backend_id"])

        # Track first response time
        issue = comment.issue
        if issue.first_response_at is None and comment.author.user != issue.caller:
            issue.first_response_at = timezone.now()
            issue.save(update_fields=["first_response_at"])

    def update_comment(self, comment):
        return

    def delete_comment(self, comment):
        return

    def create_attachment(self, attachment):
        attachment.backend_id = f"WLD-A-{attachment.uuid.hex[:8].upper()}"
        attachment.save(update_fields=["backend_id"])

    def delete_attachment(self, attachment):
        return

    def get_users(self):
        return

    def pull_priorities(self):
        return

    def create_issue_links(self, *args, **kwargs):
        return

    def get_issue_details(self):
        return {}

    def update_is_available(self, issue=None):
        return True

    def destroy_is_available(self, issue=None):
        return True

    @cached_property
    def _status_workflow(self) -> dict[str, set[str]] | None:
        """The configured workflow as from-status -> to-statuses, or None.

        Read once per backend instance: serializing a list of issues asks for
        every issue's transitions, and querying the table per issue is an N+1.
        """
        transitions: dict[str, set[str]] = {}
        for from_status, to_status in models.IssueStatusTransition.objects.values_list(
            "from_status", "to_status"
        ):
            transitions.setdefault(from_status, set()).add(to_status)
        return transitions or None

    @cached_property
    def _registered_statuses(self) -> set[str]:
        # The default status is included deliberately: `IssueStatus` only ever
        # holds terminal statuses, so without it a ticket resolved by mistake
        # could never be reopened on a deployment that configured no workflow.
        return {self.default_status} | set(
            models.IssueStatus.objects.values_list("name", flat=True)
        )

    def get_available_statuses(self, issue) -> list[str]:
        """Statuses this issue may move to, per the configured workflow.

        `IssueStatusTransition` is the workflow definition. When an operator has
        not defined one, `is_transition_allowed` permits everything, so offer
        the registered statuses instead — that is at least Resolved and Canceled
        on a deployment that configured the terminal statuses at all.
        """
        if self._status_workflow is not None:
            candidates = self._status_workflow.get(issue.status, set())
        else:
            candidates = self._registered_statuses
        return sorted(candidates - {issue.status})

    def attachment_destroy_is_available(self, attachment=None):
        return True

    def _set_sla_deadlines(self, issue):
        response_hours = config.WALDUR_SUPPORT_SLA_RESPONSE_HOURS
        resolution_hours = config.WALDUR_SUPPORT_SLA_RESOLUTION_HOURS
        now = timezone.now()
        issue.first_response_deadline = now + timedelta(hours=response_hours)
        issue.resolution_deadline = now + timedelta(hours=resolution_hours)

    def _auto_assign(self, issue):
        strategy = config.WALDUR_SUPPORT_AUTO_ASSIGN_STRATEGY
        if strategy == "round_robin":
            assignee = self._get_next_round_robin_assignee()
        else:
            assignee = self._get_least_loaded_assignee()

        if assignee:
            issue.assignee = assignee

    def _get_least_loaded_assignee(self):
        from django.db.models import Count, Q

        support_users = models.SupportUser.objects.filter(
            is_active=True,
            backend_name=self.backend_name,
        )
        if not support_users.exists():
            return None

        return (
            support_users.annotate(
                open_count=Count(
                    "issues",
                    filter=Q(issues__resolution_date__isnull=True),
                )
            )
            .order_by("open_count")
            .first()
        )

    def _get_next_round_robin_assignee(self):
        support_users = list(
            models.SupportUser.objects.filter(
                is_active=True,
                backend_name=self.backend_name,
            )
            .order_by("id")
            .values_list("id", flat=True)
        )
        if not support_users:
            return None

        cache_key = "basic_backend_round_robin_index"
        current_index = cache.get(cache_key, 0)
        next_index = (current_index + 1) % len(support_users)
        cache.set(cache_key, next_index, timeout=None)

        return models.SupportUser.objects.get(id=support_users[current_index])
