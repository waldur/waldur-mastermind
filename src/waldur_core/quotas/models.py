"""
Quota limit is not updated concurrently.
Quota usage is updated concurrently.
In order to avoid shared write deadlock we use INSERT instead of UPDATE statement.
That's why for usage we store delta instead of aggregated SUM value.
And we use SUM function when we read quota usage.
"""
import inspect
import logging

from django.contrib.contenttypes import fields as ct_fields
from django.contrib.contenttypes import models as ct_models
from django.db import models, transaction
from django.db.models import Sum
from django.utils.translation import gettext_lazy as _
from model_utils import FieldTracker

from waldur_core.quotas import exceptions, fields, managers

logger = logging.getLogger(__name__)


class QuotaLimit(models.Model):
    name = models.CharField(max_length=150, db_index=True)
    value = models.BigIntegerField(default=-1)

    content_type = models.ForeignKey(
        on_delete=models.CASCADE, to=ct_models.ContentType, null=True
    )
    object_id = models.PositiveIntegerField(null=True)
    scope = ct_fields.GenericForeignKey('content_type', 'object_id')

    tracker = FieldTracker(fields=['value'])
    objects = managers.QuotaManager('scope')

    class Meta:
        unique_together = (('name', 'content_type', 'object_id'),)


class QuotaUsage(models.Model):
    name = models.CharField(max_length=150, db_index=True)
    delta = models.BigIntegerField(default=0)

    content_type = models.ForeignKey(
        on_delete=models.CASCADE, to=ct_models.ContentType, null=True
    )
    object_id = models.PositiveIntegerField(null=True)
    scope = ct_fields.GenericForeignKey('content_type', 'object_id')

    objects = managers.QuotaManager('scope')


class QuotaModelMixin(models.Model):
    """
    Add general fields and methods to model for quotas usage.

    Model with quotas have inherit this mixin.
    For quotas implementation such methods and fields have to be defined:
      - class Quota(QuotaModelMixin) - class with quotas fields as attributes.

    Example:
        Customer(models.Model):
            ...
            Quotas(quotas_models.QuotaModelMixin.Quotas):
                nc_user_count = quotas_fields.QuotaField()  # define user count quota for customers

    Use such methods to change objects quotas:
      set_quota_limit, set_quota_usage, add_quota_usage.
    """

    class Quotas(metaclass=fields.FieldsContainerMeta):
        enable_fields_caching = True
        # register model quota fields here

    class Meta:
        abstract = True

    def get_quota_limit(self, quota_name):
        try:
            return QuotaLimit.objects.get(scope=self, name=quota_name).value
        except QuotaLimit.DoesNotExist:
            field = getattr(self.Quotas, quota_name, None)
            if field:
                return field.default_limit
            return -1

    def set_quota_limit(self, quota_name, limit):
        QuotaLimit.objects.update_or_create(
            object_id=self.id,
            content_type=ct_models.ContentType.objects.get_for_model(self),
            name=quota_name,
            defaults={'value': limit},
        )

    def get_quota_usage(self, quota_name):
        qs = QuotaUsage.objects.filter(scope=self, name=quota_name)
        return max(
            0,
            qs.aggregate(sum=Sum('delta'))['sum'] or 0,
        )

    @transaction.atomic
    def set_quota_usage(self, quota_name, usage):
        current = self.get_quota_usage(quota_name)
        self.add_quota_usage(quota_name, usage - current)

    def add_quota_usage(self, quota_name, delta, validate=False):
        if validate:
            self.validate_quota_change({quota_name: delta})
        QuotaUsage.objects.create(scope=self, name=quota_name, delta=delta)

    def apply_quota_usage(self, quota_deltas):
        for name, delta in quota_deltas.items():
            QuotaUsage.objects.create(scope=self, name=name, delta=delta)

    def validate_quota_change(self, quota_deltas):
        """
        Get error messages about object and his ancestor quotas that will be exceeded if quota_delta will be added.

        raise_exception - if True QuotaValidationError will be raised if validation fails
        quota_deltas - dictionary of quotas deltas, example:
        {
            'ram': 1024,
            'storage': 2048,
            ...
        }
        Example of output:
            ['ram quota limit: 1024, requires: 2048(instance#1)', ...]

        """
        errors = []
        for name, delta in quota_deltas.items():
            if not delta:
                continue
            usage = self.get_quota_usage(name)
            limit = self.get_quota_limit(name)
            if limit == -1:
                continue
            if usage + delta > limit:
                errors.append(f'{name} quota limit: {limit}, requires {usage + delta}')
        if errors:
            raise exceptions.QuotaValidationError(
                _('One or more quotas were exceeded: %s') % ';'.join(errors)
            )

    @classmethod
    def get_quotas_fields(cls, field_class=None) -> list[fields.QuotaField]:
        if not hasattr(cls, '_quota_fields') or not cls.Quotas.enable_fields_caching:
            cls._quota_fields = dict(
                inspect.getmembers(
                    cls.Quotas, lambda m: isinstance(m, fields.QuotaField)
                )
            ).values()
        if field_class is not None:
            return [v for v in cls._quota_fields if isinstance(v, field_class)]
        return cls._quota_fields

    @classmethod
    def get_quotas_names(cls):
        return [f.name for f in cls.get_quotas_fields()]

    @property
    def quota_usages(self):
        return {
            row['name']: row['value'] or 0
            for row in QuotaUsage.objects.filter(scope=self)
            .values('name')
            .annotate(value=Sum('delta'))
        }

    @property
    def quota_limits(self):
        return {
            row['name']: row['value'] or -1
            for row in QuotaLimit.objects.filter(scope=self).values('name', 'value')
        }

    @property
    def quotas(self):
        usages = self.quota_usages
        limits = self.quota_limits
        return [
            {
                'name': name,
                'usage': usages.get(name) or 0,
                'limit': limits.get(name) or -1,
            }
            for name in self.get_quotas_names()
        ]


class ExtendableQuotaModelMixin(QuotaModelMixin):
    """Allows to add quotas to model in runtime.

    Example:
        from waldur_core.quotas.fields import QuotaField

        QuotaScopeModel.add_quota_field(
            name='quota_name',
            quota_field=QuotaField(...),
        )
    """

    class Quotas(QuotaModelMixin.Quotas):
        enable_fields_caching = False
        # register model quota fields here

    class Meta:
        abstract = True

    @classmethod
    def add_quota_field(cls, name, quota_field):
        # We need to initiate name field here because quota is not listed in Quotas class
        # and initialization is not executed automatically.
        quota_field.name = name
        setattr(cls.Quotas, name, quota_field)
        from waldur_core.quotas.apps import QuotasConfig

        # For counter quotas we need to register signals explicitly
        if isinstance(quota_field, fields.CounterQuotaField):
            QuotasConfig.register_counter_field_signals(
                model=cls, counter_field=quota_field
            )


class SharedQuotaMixin:
    """
    This mixin updates quotas for several scopes.
    """

    def get_quota_deltas(self):
        """
        This method should return dict where key is quota name and value is quota diff.
        For example:
        {
            'storage': 1024,
            'volumes': 1,
        }
        """
        raise NotImplementedError()

    def get_quota_scopes(self):
        """
        This method should return list of quota model mixins.
        """
        raise NotImplementedError()

    def apply_quota_changes(self, mult=1, validate=False):
        scopes = self.get_quota_scopes()
        deltas = self.get_quota_deltas()
        for name, delta in deltas.items():
            for scope in scopes:
                if scope:
                    scope.add_quota_usage(name, delta * mult, validate=validate)

    def increase_backend_quotas_usage(self, validate=False):
        self.apply_quota_changes(validate=validate)

    def decrease_backend_quotas_usage(self):
        self.apply_quota_changes(mult=-1)
