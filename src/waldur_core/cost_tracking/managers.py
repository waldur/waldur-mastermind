import datetime

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db import models as django_models
from django.db.models import Q
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.core.managers import GenericKeyMixin
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.models import Service


# TODO: This mixin duplicates quota filter manager - they need to be moved to core (NC-686)
class UserFilterMixin(object):

    def filtered_for_user(self, user, queryset=None):
        if queryset is None:
            queryset = self.get_queryset()

        if user.is_staff or user.is_support:
            return queryset

        query = Q()
        for model in self.get_available_models():
            user_object_ids = filter_queryset_for_user(model.objects.all(), user).values_list('id', flat=True)
            content_type_id = ContentType.objects.get_for_model(model).id
            query |= Q(object_id__in=list(user_object_ids), content_type_id=content_type_id)

        return queryset.filter(query)

    def get_available_models(self):
        """ Return list of models that are acceptable """
        raise NotImplementedError()


class PriceEstimateManager(GenericKeyMixin, UserFilterMixin, django_models.Manager):

    def get_available_models(self):
        """ Return list of models that are acceptable """
        return self.model.get_estimated_models()

    def get_current(self, scope):
        now = timezone.now()
        return self.get(scope=scope, year=now.year, month=now.month)

    def get_or_create_current(self, scope):
        now = timezone.now()
        return self.get_or_create(scope=scope, month=now.month, year=now.year)

    def filter_current(self):
        now = timezone.now()
        return self.filter(year=now.year, month=now.month)


class ConsumptionDetailsQuerySet(django_models.QuerySet):

    def create(self, price_estimate):
        """ Take configuration from previous month, it it exists.
            Set last_update_time equals to the beginning of the month.
        """
        kwargs = {}
        try:
            previous_price_estimate = price_estimate.get_previous()
        except ObjectDoesNotExist:
            pass
        else:
            configuration = previous_price_estimate.consumption_details.configuration
            kwargs['configuration'] = configuration
        month_start = core_utils.month_start(datetime.date(price_estimate.year, price_estimate.month, 1))
        kwargs['last_update_time'] = month_start
        return super(ConsumptionDetailsQuerySet, self).create(price_estimate=price_estimate, **kwargs)


ConsumptionDetailsManager = django_models.Manager.from_queryset(ConsumptionDetailsQuerySet)


class PriceListItemManager(GenericKeyMixin, UserFilterMixin, django_models.Manager):

    def get_available_models(self):
        """ Return list of models that are acceptable """
        return Service.get_all_models()
