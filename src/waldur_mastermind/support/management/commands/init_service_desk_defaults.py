from constance import config
from django.core.management.base import BaseCommand
from django.db import transaction

from waldur_mastermind.support import models
from waldur_mastermind.support.backend import SupportBackendType
from waldur_mastermind.support.enums import ISSUE_STATUS_TYPE_CHOICES

#: The built-in service desk reads the statuses a ticket may move to out of
#: `IssueStatus`, and refuses a ticket whose type is not an active
#: `RequestType`. Neither table ships with content, so an unseeded deployment
#: cannot create a ticket at all, and staff are offered no status on the
#: tickets it already has. `IssueStatus.check_success_status` also logs
#: CRITICAL until a row of each terminal type exists.
#:
#: The status names come from the type labels, so the seeded rows cannot drift
#: from the enum they are classified by.
DEFAULT_STATUSES = tuple((label, type_) for type_, label in ISSUE_STATUS_TYPE_CHOICES)

DEFAULT_REQUEST_TYPE = "Service Request"

#: The one backend whose tickets Waldur owns end to end. A remote service desk
#: brings its own request types, and seeding one there is actively harmful: the
#: Atlassian backend falls back to pulling types from Jira only while it finds
#: none locally, so a seeded row suppresses that pull and hands a null backend
#: id to ticket creation.
LOCAL_BACKENDS = (SupportBackendType.BASIC,)


class Command(BaseCommand):
    help = (
        "Seed the terminal issue statuses, and the default request type used by "
        "the built-in service desk. Existing rows are left untouched, so the "
        "command is safe to re-run. Request types are seeded only when the "
        "active backend is one Waldur owns; a deployment backed by a remote "
        "service desk gets its types from there."
    )

    def handle(self, *args, **options):
        # One transaction: a run that dies between the two statuses would
        # otherwise leave the table with a single terminal type, which is worse
        # than leaving it empty. `check_success_status` then returns None for
        # every ticket and logs CRITICAL, and `Issue.set_canceled` raises.
        with transaction.atomic():
            self.seed_statuses()
            self.seed_request_type()

    def seed_statuses(self):
        created = []
        for name, type_ in DEFAULT_STATUSES:
            # Per type, not per table: an operator who added only a resolved
            # status, or a run that failed halfway, leaves the deployment in
            # exactly the half-configured state this command exists to avoid.
            if models.IssueStatus.objects.filter(type=type_).exists():
                continue
            models.IssueStatus.objects.get_or_create(
                name=name, defaults={"type": type_}
            )
            created.append(name)

        if created:
            self.stdout.write(
                self.style.SUCCESS("Created issue statuses: %s." % ", ".join(created))
            )
        else:
            self.stdout.write(
                "Issue statuses are already configured, leaving them as they are."
            )

    def seed_request_type(self):
        backend_type = config.WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE
        if backend_type not in LOCAL_BACKENDS:
            self.stdout.write(
                "Active backend is %s, which brings its own request types; "
                "not seeding one." % backend_type
            )
            return

        if models.RequestType.objects.exists():
            self.stdout.write(
                "Request types are already configured, leaving them as they are."
            )
            return

        # backend_id stays null, which is how a locally created type is marked.
        # backend_name is set here rather than left to the model's save(), which
        # would stamp whatever the active backend happened to be at seed time.
        models.RequestType.objects.create(
            name=DEFAULT_REQUEST_TYPE,
            issue_type_name=DEFAULT_REQUEST_TYPE,
            backend_name=backend_type,
            is_active=True,
            order=0,
        )

        self.stdout.write(
            self.style.SUCCESS("Created request type: %s." % DEFAULT_REQUEST_TYPE)
        )
