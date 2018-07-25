from __future__ import unicode_literals

from collections import defaultdict
import functools
from functools import reduce
import inspect
import logging

from django.contrib.contenttypes import fields as ct_fields
from django.contrib.contenttypes import models as ct_models
from django.db import models
from django.db.models import Sum
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _
from model_utils import FieldTracker
from reversion import revisions as reversion
import six

from waldur_core.core.models import UuidMixin, ReversionMixin, DescendantMixin
from waldur_core.logging.loggers import LoggableMixin
from waldur_core.logging.models import AlertThresholdMixin
from waldur_core.quotas import exceptions, managers, fields

logger = logging.getLogger(__name__)


@python_2_unicode_compatible
@reversion.register(fields=['usage', 'limit'])
class Quota(UuidMixin, AlertThresholdMixin, LoggableMixin, ReversionMixin, models.Model):
    """
    Abstract quota for any resource.

    Quota can exist without scope: for example, a quota for all projects or all
    customers on site.
    If quota limit is set to -1 quota will never be exceeded.
    """
    class Meta:
        unique_together = (('name', 'content_type', 'object_id'),)

    limit = models.FloatField(default=-1)
    usage = models.FloatField(default=0)
    name = models.CharField(max_length=150, db_index=True)

    content_type = models.ForeignKey(ct_models.ContentType, null=True)
    object_id = models.PositiveIntegerField(null=True)
    scope = ct_fields.GenericForeignKey('content_type', 'object_id')

    objects = managers.QuotaManager('scope')
    tracker = FieldTracker()

    def __str__(self):
        return '%s quota for %s' % (self.name, self.scope)

    def is_exceeded(self, delta=None, threshold=None):
        """
        Check is quota exceeded

        If delta is not None then checks if quota exceeds with additional delta usage
        If threshold is not None then checks if quota usage over threshold * limit
        """
        if self.limit == -1:
            return False

        # Allow to decrease quota usage
        if delta < 0:
            return False

        usage = self.usage
        limit = self.limit

        if delta is not None:
            usage += delta
        if threshold is not None:
            limit = threshold * limit

        return usage > limit

    def get_log_fields(self):
        return ('uuid', 'name', 'limit', 'usage', 'scope')

    def get_field(self):
        fields = self.scope.get_quotas_fields()
        try:
            return next(f for f in fields if f.name == self.name)
        except StopIteration:
            return

    def is_over_threshold(self):
        return self.usage >= self.threshold


def _fail_silently(method):

    @functools.wraps(method)
    def wrapped(self, quota_name, *args, **kwargs):
        try:
            return method(self, quota_name, *args, **kwargs)
        except Quota.DoesNotExist:
            if not kwargs.get('fail_silently', False):
                raise Quota.DoesNotExist(_('Object %(object)s does not have quota with name %(name)s.') % {
                    'object': self,
                    'name': quota_name
                })

    return wrapped


class QuotaModelMixin(models.Model):
    """
    Add general fields and methods to model for quotas usage.

    Model with quotas have inherit this mixin.
    For quotas implementation such methods and fields have to be defined:
      - class Quota(QuotaModelMixin) - class with quotas fields as attributes.
      - can_user_update_quotas(self, user) - Return True if user has permission to update quotas of this object.
      - GLOBAL_COUNT_QUOTA_NAME - Name of global count quota. It presents - global quota will be automatically created
                                  for model. Optional attribute.

    Example:
        Customer(models.Model):
            ...
            Quotas(quotas_models.QuotaModelMixin.Quotas):
                nc_user_count = quotas_fields.QuotaField()  # define user count quota for customers

            # optional descriptor to direct access to quota
            nc_user_count = quotas_fields.QuotaLimitField(quota_field=Quotas.nc_user_count)

            def can_user_update_quotas(self, user):
                # only staff user can edit Customer quotas
                return user.is_staff

    Use such methods to change objects quotas:
      set_quota_limit, set_quota_usage, add_quota_usage.

    Helper methods validate_quota_change and get_sum_of_quotas_as_dict provide common operations with objects quotas.
    Check methods docstrings for more details.
    """

    class Quotas(six.with_metaclass(fields.FieldsContainerMeta)):
        enable_fields_caching = True
        # register model quota fields here

    class Meta:
        abstract = True

    quotas = ct_fields.GenericRelation('quotas.Quota', related_query_name='quotas')

    @_fail_silently
    def set_quota_limit(self, quota_name, limit, fail_silently=False):
        quota = self.quotas.get(name=quota_name)
        if quota.limit != limit:
            quota.limit = limit
            quota.save(update_fields=['limit'])

    @_fail_silently
    def set_quota_usage(self, quota_name, usage, fail_silently=False):
        quota = self.quotas.get(name=quota_name)
        if quota.usage != usage:
            quota.usage = usage
            quota.save(update_fields=['usage'])

    @_fail_silently
    def add_quota_usage(self, quota_name, usage_delta, fail_silently=False, validate=False):
        quota = self.quotas.get(name=quota_name)
        if validate and quota.is_exceeded(usage_delta):
            raise exceptions.QuotaValidationError(
                _('%(quota)s "%(name)s" quota is over limit. Required: %(usage)s, limit: %(limit)s.') % dict(
                    quota=self, name=quota_name, usage=quota.usage + usage_delta, limit=quota.limit))
        quota.usage += usage_delta
        if quota.usage < 0:
            logger.error('%(quota)s "%(name)s" quota usage should not be negative. '
                         'Current usage: %(usage)s, delta: %(usage_delta)s',
                         dict(quota=self, name=quota_name, usage=quota.usage, usage_delta=usage_delta))
            quota.usage = 0
        quota.save(update_fields=['usage'])

    def get_quota_ancestors(self):
        if isinstance(self, DescendantMixin):
            # We need to use set in order to eliminate duplicates.
            # Consider, for example, two ways of traversing from resource to customer:
            # resource -> spl -> project -> customer
            # resource -> spl -> service -> customer
            return {a for a in self.get_ancestors() if isinstance(a, QuotaModelMixin)}
        return {}

    def validate_quota_change(self, quota_deltas, raise_exception=False):
        """
        Get error messages about object and his ancestor quotas that will be exceeded if quota_delta will be added.

        raise_exception - if True QuotaExceededException will be raised if validation fails
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
        for name, delta in six.iteritems(quota_deltas):
            quota = self.quotas.get(name=name)
            if quota.is_exceeded(delta):
                errors.append('%s quota limit: %s, requires %s (%s)\n' % (
                    quota.name, quota.limit, quota.usage + delta, quota.scope))
        if not raise_exception:
            return errors
        else:
            if errors:
                raise exceptions.QuotaExceededException(_('One or more quotas were exceeded: %s') % ';'.join(errors))

    def can_user_update_quotas(self, user):
        """
        Return True if user has permission to update quota
        """
        return user.is_staff

    @classmethod
    def get_sum_of_quotas_as_dict(cls, scopes, quota_names=None, fields=['usage', 'limit']):
        """
        Return dictionary with sum of all scopes' quotas.

        Dictionary format:
        {
            'quota_name1': 'sum of limits for quotas with such quota_name1',
            'quota_name1_usage': 'sum of usages for quotas with such quota_name1',
            ...
        }
        All `scopes` have to be instances of the same model.
        `fields` keyword argument defines sum of which fields of quotas will present in result.
        """
        if not scopes:
            return {}

        if quota_names is None:
            quota_names = cls.get_quotas_names()

        scope_models = set([scope._meta.model for scope in scopes])
        if len(scope_models) > 1:
            raise exceptions.QuotaError(_('All scopes have to be instances of the same model.'))

        filter_kwargs = {
            'content_type': ct_models.ContentType.objects.get_for_model(scopes[0]),
            'object_id__in': [scope.id for scope in scopes],
            'name__in': quota_names
        }

        result = {}
        if 'usage' in fields:
            items = Quota.objects.filter(**filter_kwargs)\
                         .values('name').annotate(usage=Sum('usage'))
            for item in items:
                result[item['name'] + '_usage'] = item['usage']

        if 'limit' in fields:
            unlimited_quotas = Quota.objects.filter(limit=-1, **filter_kwargs)
            unlimited_quotas = list(unlimited_quotas.values_list('name', flat=True))
            for quota_name in unlimited_quotas:
                result[quota_name] = -1

            items = Quota.objects\
                         .filter(**filter_kwargs)\
                         .exclude(name__in=unlimited_quotas)\
                         .values('name')\
                         .annotate(limit=Sum('limit'))
            for item in items:
                result[item['name']] = item['limit']

        return result

    @classmethod
    def get_sum_of_quotas_for_querysets(cls, querysets, quota_names=None):
        partial_sums = [qs.model.get_sum_of_quotas_as_dict(qs, quota_names) for qs in querysets]
        return reduce(cls._sum_dicts, partial_sums, defaultdict(lambda: 0))

    @classmethod
    def _sum_dicts(cls, total, partial):
        for key, val in partial.items():
            if val != -1:
                total[key] += val
        for key in partial.keys():
            if key not in total:
                total[key] = -1
        return total

    @classmethod
    def get_quotas_fields(cls, field_class=None):
        if not hasattr(cls, '_quota_fields') or not cls.Quotas.enable_fields_caching:
            cls._quota_fields = dict(inspect.getmembers(cls.Quotas, lambda m: isinstance(m, fields.QuotaField))).values()
        if field_class is not None:
            return [v for v in cls._quota_fields if isinstance(v, field_class)]
        return cls._quota_fields

    @classmethod
    def get_quotas_names(cls):
        return [f.name for f in cls.get_quotas_fields()]


class ExtendableQuotaModelMixin(QuotaModelMixin):
    """ Allows to add quotas to model in runtime.

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
            QuotasConfig.register_counter_field_signals(model=cls, counter_field=quota_field)
