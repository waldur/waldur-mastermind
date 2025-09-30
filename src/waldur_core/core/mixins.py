import logging
from functools import wraps

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models as django_models
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_fsm import FSMIntegerField
from model_utils.models import TimeStampedModel
from rest_framework import response, status

from waldur_core.core.enums import CoreStates, ReviewStates
from waldur_core.core.models import User


def ensure_atomic_transaction(func):
    @wraps(func)
    def wrapped(self, *args, **kwargs):
        if settings.WALDUR_CORE["USE_ATOMIC_TRANSACTION"]:
            with transaction.atomic():
                return func(self, *args, **kwargs)
        else:
            return func(self, *args, **kwargs)

    return wrapped


class AsyncExecutor:
    async_executor = True


class CreateExecutorMixin(AsyncExecutor):
    """Mixin to execute create operations using background executors."""

    create_executor = NotImplemented

    @ensure_atomic_transaction
    def perform_create(self, serializer):
        instance = serializer.save()
        self.create_executor.execute(instance, is_async=self.async_executor)
        instance.refresh_from_db()


class UpdateExecutorMixin(AsyncExecutor):
    """Mixin to execute update operations using background executors."""

    update_executor = NotImplemented

    def get_update_executor_kwargs(self, serializer):
        return {}

    @ensure_atomic_transaction
    def perform_update(self, serializer):
        instance = self.get_object()
        # Save all instance fields before update.
        # To avoid additional DB queries - store foreign keys as ids.
        # Warning! M2M fields will be ignored.
        before_update_fields = {
            f: getattr(instance, f.attname) for f in instance._meta.fields
        }
        super().perform_update(serializer)
        instance.refresh_from_db()
        updated_fields = {
            f.name
            for f, v in before_update_fields.items()
            if v != getattr(instance, f.attname)
        }
        kwargs = self.get_update_executor_kwargs(serializer)

        self.update_executor.execute(
            instance,
            is_async=self.async_executor,
            updated_fields=updated_fields,
            **kwargs,
        )
        serializer.instance.refresh_from_db()


class DeleteExecutorMixin(AsyncExecutor):
    """Mixin to execute delete operations using background executors."""

    delete_executor = NotImplemented

    @ensure_atomic_transaction
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        self.delete_executor.execute(
            instance,
            is_async=self.async_executor,
            force=instance.state == CoreStates.ERRED,
        )
        return response.Response(
            {"detail": _("Deletion was scheduled.")}, status=status.HTTP_202_ACCEPTED
        )


class ExecutorMixin(CreateExecutorMixin, UpdateExecutorMixin, DeleteExecutorMixin):
    """Execute create/update/delete operation with executor"""

    pass


class EagerLoadMixin:
    """Reduce number of requests to DB.

    Serializer should implement static method "eager_load", that selects
    objects that are necessary for serialization.
    """

    def get_queryset(self):
        queryset = super().get_queryset()
        serializer_class = self.get_serializer_class()
        if self.action in ("list", "retrieve") and hasattr(
            serializer_class, "eager_load"
        ):
            queryset = serializer_class.eager_load(queryset, self.request)
        return queryset


class ScopeMixin(django_models.Model):
    class Meta:
        abstract = True

    content_type = django_models.ForeignKey(
        to=ContentType,
        on_delete=django_models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    object_id = django_models.PositiveIntegerField(null=True, blank=True)
    scope = GenericForeignKey("content_type", "object_id")


class ReviewStateMixin(django_models.Model):
    class Meta:
        abstract = True

    state = FSMIntegerField(default=ReviewStates.DRAFT, choices=ReviewStates.CHOICES)

    def submit(self):
        self.state = ReviewStates.PENDING
        self.save(update_fields=["state"])

    def cancel(self):
        self.state = ReviewStates.CANCELED
        self.save(update_fields=["state"])


class ReviewMixin(ReviewStateMixin, TimeStampedModel):
    class Meta:
        abstract = True

    reviewed_by = django_models.ForeignKey(
        on_delete=django_models.CASCADE,
        to=User,
        null=True,
        blank=True,
        related_name="+",
        help_text="User who performed the review",
    )

    reviewed_at = django_models.DateTimeField(
        editable=False,
        null=True,
        blank=True,
        help_text="Timestamp when the review was completed",
    )

    review_comment = django_models.TextField(
        null=True,
        blank=True,
        help_text="Optional comment provided during review",
    )

    @transaction.atomic
    def approve(self, user, comment=None):
        self.reviewed_by = user
        self.review_comment = comment
        self.reviewed_at = timezone.now()
        self.state = ReviewStates.APPROVED
        self.save(
            update_fields=["reviewed_by", "reviewed_at", "review_comment", "state"]
        )

    @transaction.atomic
    def reject(self, user, comment=None):
        self.reviewed_by = user
        self.review_comment = comment
        self.reviewed_at = timezone.now()
        self.state = ReviewStates.REJECTED
        self.save(
            update_fields=["reviewed_by", "reviewed_at", "review_comment", "state"]
        )

    @property
    def is_rejected(self):
        return self.state == ReviewStates.REJECTED


class GetValueMixin:
    """Mixin to provide helper method for getting values from attrs or instance."""

    def get_from_attrs_or_instance(self, attrs, field_name, default=None):
        return attrs.get(field_name, getattr(self.instance, field_name, default))


class ProjectNameTemplateMixin(django_models.Model):
    """Mixin for models that need to generate project names from templates."""

    class Meta:
        abstract = True

    project_name_template = django_models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Template for project name. Supports {username}, {email}, {full_name} variables",
    )

    def resolve_project_name(self, user):
        """
        Resolve project name using template or default to username.

        Args:
            user: User instance for template variable substitution

        Returns:
            str: Resolved project name
        """
        if self.project_name_template:
            try:
                return self.project_name_template.format(
                    username=user.username,
                    email=user.email,
                    full_name=user.get_full_name() or user.username,
                )
            except (KeyError, AttributeError) as e:
                logger = logging.getLogger(__name__)
                logger.error(
                    f"Failed to format project name template '{self.project_name_template}' "
                    f"for user {user.username}: {e}. Falling back to username. "
                    f"Valid placeholders are: username, email, full_name"
                )
                # Fall back to username if template evaluation fails
                return user.username
        return user.username
