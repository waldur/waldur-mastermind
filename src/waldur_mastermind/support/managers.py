from django.db import models as django_models
from django.db.models import Q

from waldur_core.core.models import User
from waldur_core.core.permissions import has_staff_access
from waldur_core.permissions.enums import RoleEnum
from waldur_core.structure.managers import get_connected_customers
from waldur_mastermind.support import backend, models


class SupportUserQuerySet(django_models.QuerySet):
    def get_or_create_from_user(self, user):
        """Get or create support user based on regular user"""
        return self.get_or_create(
            user=user,
            backend_name=backend.get_active_backend().backend_name,
            defaults={"name": user.full_name or user.username},
        )


SupportUserManager = django_models.Manager.from_queryset(SupportUserQuerySet)


class AttachmentQuerySet(django_models.QuerySet):
    def filter_for_user(self, principal):
        """Attachments of issues the user's customers own or the user called.

        ``principal`` is a request or a user. A request acts as staff only if
        a personal access token behind it carries the staff scope.
        """
        if isinstance(principal, User):
            user, acts_as_staff = principal, principal.is_staff
        else:
            user, acts_as_staff = principal.user, has_staff_access(principal)
        if not acts_as_staff:
            user_customers = get_connected_customers(user, RoleEnum.CUSTOMER_OWNER)
            subquery = Q(issue__customer__in=user_customers) | Q(issue__caller=user)
            return self.filter(subquery)
        return self


AttachmentManager = django_models.Manager.from_queryset(AttachmentQuerySet)


class IssueQuerySet(django_models.QuerySet):
    """One definition of open/closed, shared by the statistics and the filters.

    Terminal statuses are looked up by *name* from the `IssueStatus` registry.
    The old inline version compared `status` — a CharField holding the status
    name — against `IssueStatus.Types.RESOLVED` and `CANCELED`, which are the
    integers 0 and 1, so it never excluded anything.
    """

    def _terminal_status_names(self):
        return models.IssueStatus.objects.filter(
            type__in=[
                models.IssueStatus.Types.RESOLVED,
                models.IssueStatus.Types.CANCELED,
            ]
        ).values_list("name", flat=True)

    def open(self):
        return self.exclude(status__in=self._terminal_status_names()).filter(
            resolution_date__isnull=True
        )

    def closed(self):
        return self.filter(
            Q(status__in=self._terminal_status_names())
            | Q(resolution_date__isnull=False)
        )


IssueManager = django_models.Manager.from_queryset(IssueQuerySet)
