from functools import wraps

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models as django_models
from django.db import transaction
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from django_fsm import FSMIntegerField
from model_utils.models import TimeStampedModel
from rest_framework import response, status

from waldur_core.core import models

User = get_user_model()


def ensure_atomic_transaction(func):
    @wraps(func)
    def wrapped(self, *args, **kwargs):
        if settings.WALDUR_CORE['USE_ATOMIC_TRANSACTION']:
            with transaction.atomic():
                return func(self, *args, **kwargs)
        else:
            return func(self, *args, **kwargs)

    return wrapped


class AsyncExecutor:
    async_executor = True


class CreateExecutorMixin(AsyncExecutor):
    create_executor = NotImplemented

    @ensure_atomic_transaction
    def perform_create(self, serializer):
        instance = serializer.save()
        self.create_executor.execute(instance, is_async=self.async_executor)
        instance.refresh_from_db()


class UpdateExecutorMixin(AsyncExecutor):
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
        super(UpdateExecutorMixin, self).perform_update(serializer)
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
            **kwargs
        )
        serializer.instance.refresh_from_db()


class DeleteExecutorMixin(AsyncExecutor):
    delete_executor = NotImplemented

    @ensure_atomic_transaction
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        self.delete_executor.execute(
            instance,
            is_async=self.async_executor,
            force=instance.state == models.StateMixin.States.ERRED,
        )
        return response.Response(
            {'detail': _('Deletion was scheduled.')}, status=status.HTTP_202_ACCEPTED
        )


class ExecutorMixin(CreateExecutorMixin, UpdateExecutorMixin, DeleteExecutorMixin):
    """ Execute create/update/delete operation with executor """

    pass


class EagerLoadMixin:
    """ Reduce number of requests to DB.

        Serializer should implement static method "eager_load", that selects
        objects that are necessary for serialization.
    """

    def get_queryset(self):
        queryset = super(EagerLoadMixin, self).get_queryset()
        serializer_class = self.get_serializer_class()
        if self.action in ('list', 'retrieve') and hasattr(
            serializer_class, 'eager_load'
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
        related_name='+',
    )
    object_id = django_models.PositiveIntegerField(null=True, blank=True)
    scope = GenericForeignKey('content_type', 'object_id')


class ReviewStateMixin(django_models.Model):
    class Meta:
        abstract = True

    class States:
        DRAFT = 1
        PENDING = 2
        APPROVED = 3
        REJECTED = 4
        CANCELED = 5

        CHOICES = (
            (DRAFT, 'draft'),
            (PENDING, 'pending'),
            (APPROVED, 'approved'),
            (REJECTED, 'rejected'),
            (CANCELED, 'canceled'),
        )

    state = FSMIntegerField(default=States.DRAFT, choices=States.CHOICES)

    def submit(self):
        self.state = self.States.PENDING
        self.save(update_fields=['state'])

    def cancel(self):
        self.state = self.States.CANCELED
        self.save(update_fields=['state'])


class ReviewMixin(ReviewStateMixin, TimeStampedModel):
    class Meta:
        abstract = True

    reviewed_by = django_models.ForeignKey(
        on_delete=django_models.CASCADE,
        to=User,
        null=True,
        blank=True,
        related_name='+',
    )

    reviewed_at = django_models.DateTimeField(editable=False, null=True, blank=True)

    review_comment = django_models.TextField(null=True, blank=True)

    @transaction.atomic
    def approve(self, user, comment=None):
        self.reviewed_by = user
        self.review_comment = comment
        self.reviewed_at = timezone.now()
        self.state = self.States.APPROVED
        self.save(
            update_fields=['reviewed_by', 'reviewed_at', 'review_comment', 'state']
        )

    @transaction.atomic
    def reject(self, user, comment=None):
        self.reviewed_by = user
        self.review_comment = comment
        self.reviewed_at = timezone.now()
        self.state = self.States.REJECTED
        self.save(
            update_fields=['reviewed_by', 'reviewed_at', 'review_comment', 'state']
        )

    @property
    def is_rejected(self):
        return self.state == self.States.REJECTED
