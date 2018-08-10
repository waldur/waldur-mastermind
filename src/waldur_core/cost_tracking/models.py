from __future__ import unicode_literals

import datetime
import logging

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.encoding import python_2_unicode_compatible
from django.utils.lru_cache import lru_cache
from django.utils.translation import ugettext_lazy as _
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel
import six

from waldur_core.core import models as core_models, utils as core_utils
from waldur_core.core.fields import JSONField
from waldur_core.cost_tracking import managers, ConsumableItem
from waldur_core.logging.loggers import LoggableMixin
from waldur_core.structure import models as structure_models, SupportedServices

from . import CostTrackingRegister

logger = logging.getLogger(__name__)


class EstimateUpdateError(Exception):
    pass


@python_2_unicode_compatible
class PriceEstimate(LoggableMixin, core_models.UuidMixin, core_models.DescendantMixin):
    """ Store prices based on both estimates and actual consumption.

        Every record holds a list of children estimates.
                   /--- Service ---\
        Customer --                 ---> SPL --> Resource
                   \--- Project ---/
        Only resource node has actual data.
        Another ones should be re-calculated on every change of resource estimate.
    """
    content_type = models.ForeignKey(ContentType, null=True, related_name='+')
    object_id = models.PositiveIntegerField(null=True)
    scope = GenericForeignKey('content_type', 'object_id')
    details = JSONField(default=dict, help_text=_('Saved scope details. Field is populated on scope deletion.'))
    parents = models.ManyToManyField('PriceEstimate', related_name='children', help_text=_('Price estimate parents'))

    total = models.FloatField(default=0, help_text=_('Predicted price for scope for current month.'))
    consumed = models.FloatField(default=0, help_text=_('Price for resource until now.'))

    month = models.PositiveSmallIntegerField(validators=[MaxValueValidator(12), MinValueValidator(1)])
    year = models.PositiveSmallIntegerField()

    objects = managers.PriceEstimateManager('scope')

    tracker = FieldTracker()

    class Meta:
        unique_together = ('content_type', 'object_id', 'month', 'year',)

    def __str__(self):
        name = self.get_scope_name() if self.scope else self.details.get('name')
        return '%s for %s-%s %.2f' % (name, self.year, self.month, self.total)

    @classmethod
    @lru_cache(maxsize=1)
    def get_estimated_models(cls):
        return (
            structure_models.ResourceMixin.get_all_models() +
            structure_models.ServiceProjectLink.get_all_models() +
            structure_models.Service.get_all_models() +
            [structure_models.ServiceSettings] +
            [structure_models.Project, structure_models.Customer]
        )

    def get_parents(self):  # For DescendantMixin
        return self.parents.all()

    def get_children(self):  # For DescendantMixin
        return self.children.all()

    def get_log_fields(self):  # For LoggableMixin
        return 'uuid', 'scope', 'total', 'consumed'

    def is_resource_estimate(self):
        return issubclass(self.content_type.model_class(), structure_models.ResourceMixin)

    def get_previous(self):
        """ Get estimate for the same scope for previous month. """
        month, year = (self.month - 1, self.year) if self.month != 1 else (12, self.year - 1)
        return PriceEstimate.objects.get(scope=self.scope, month=month, year=year)

    def create_ancestors(self):
        """ Create price estimates for scope ancestors if they do not exist """
        if not isinstance(self.scope, core_models.DescendantMixin):
            return
        scope_parents = self.scope.get_parents()
        for scope_parent in scope_parents:
            parent, created = PriceEstimate.objects.get_or_create(scope=scope_parent, month=self.month, year=self.year)
            self.parents.add(parent)
            if created:
                parent.create_ancestors()

    def init_details(self):
        """ Initialize price estimate details based on its scope """
        self.details = {
            'name': self.get_scope_name(),
            'description': getattr(self.scope, 'description', ''),
        }
        if hasattr(self.scope, 'backend_id'):
            self.details['backend_id'] = self.scope.backend_id
        if self.is_resource_estimate():
            self.details['service_settings_name'] = self.scope.service_project_link.service.settings.name
            self.details['project_name'] = self.scope.service_project_link.project.name
        self.save(update_fields=['details'])

    def update_total(self, update_ancestors=True, raise_exception=False):
        """ Re-calculate price of resource and its ancestors for the whole month,
            based on its configuration and consumption details.
        """
        self._check_is_updatable()
        new_total = self._get_price(self.consumption_details.consumed_in_month)
        diff = new_total - self.total
        with transaction.atomic():
            self.total = new_total
            self.save(update_fields=['total'])
            if update_ancestors:
                self.update_ancestors_total(diff, raise_exception=raise_exception)

    def update_ancestors_total(self, diff, raise_exception=False):
        for ancestor in self.get_ancestors():
            ancestor.total += diff
            ancestor.save(update_fields=['total'])

    def update_consumed(self):
        """ Re-calculate price of resource until now. Does not update ancestors. """
        self._check_is_updatable()
        self.consumed = self._get_price(self.consumption_details.consumed_until_now)
        self.save(update_fields=['consumed'])

    @classmethod
    def create_historical(cls, resource, configuration, date):
        """ Create price estimate and consumption details backdating.

            Method assumes that resource had given configuration from given date
            to the end of the month.
        """
        price_estimate = cls.objects.create(scope=resource, month=date.month, year=date.year)
        price_estimate.create_ancestors()
        # configuration is updated directly because we want to avoid recalculation
        # of consumed items based on current time.
        details = ConsumptionDetails(
            price_estimate=price_estimate,
            configuration=configuration,
            last_update_time=date,
        )
        details.save()
        price_estimate.update_total()
        return price_estimate

    def _get_price(self, consumed):
        """ Calculate price estimate for scope depends on consumed data and price list items.
            Map each consumable to price list item and multiply price its price by time of usage.
        """
        price_list_items = PriceListItem.get_for_resource(self.scope)
        consumables_prices = {(item.item_type, item.key): item.minute_rate for item in price_list_items}
        total = 0
        for consumable_item, usage in consumed.items():
            try:
                total += consumables_prices[(consumable_item.item_type, consumable_item.key)] * usage
            except KeyError:
                logger.debug('Price list item for consumable "%s" does not exist.' % consumable_item)
        return total

    def _check_is_updatable(self):
        """ Raise error if price estimate does not have consumption details or
            does not belong to resource
        """
        if not ConsumptionDetails.objects.filter(price_estimate=self).exists():
            raise EstimateUpdateError('Cannot update consumed for price estimate that does not have consumption details.')
        if not self.is_resource_estimate() or not self.scope:
            raise EstimateUpdateError('Cannot update consumed for price estimate that is not related to resource.')

    def get_scope_name(self):
        if self.scope:
            if isinstance(self.scope, (structure_models.ServiceProjectLink, structure_models.Service)):
                # We need to display some meaningful name for SPL.
                return six.text_type(self.scope)
            else:
                return self.scope.name
        else:
            return self.details.get('name')

    def collect_children(self):
        """
        Recursively collect children estimates. Returns generator.
        """
        for child in self.children.filter():
            yield child
            for grandchild in child.collect_children():
                yield grandchild

    @staticmethod
    def update_resource_estimate(resource, new_configuration, raise_exception=False):
        """ Create or update price estimate for resource based on its current configuration """
        price_estimate, created = PriceEstimate.objects.get_or_create_current(scope=resource)
        if created:
            price_estimate.create_ancestors()
        consumption_details, _ = ConsumptionDetails.objects.get_or_create(price_estimate=price_estimate)
        is_updated = consumption_details.update_configuration(new_configuration)
        if is_updated:
            price_estimate.update_total(raise_exception=raise_exception)
        return price_estimate


class ConsumptionDetailUpdateError(Exception):
    pass


class ConsumptionDetailCalculateError(Exception):
    pass


class ConsumableItemsField(JSONField):
    """ Store consumable items and their usage as JSON.

        Represent data in format:
        {
            <ConsumableItem instance>: <usage>,
            <ConsumableItem instance>: <usage>,
            ...
        }
        Store data in format:
        [
            {
                "item_type": xx,
                "key": xx,
                "usage": xx,
            }
            ...
        ]
    """

    def to_python(self, value):
        value = super(ConsumableItemsField, self).to_python(value)
        if isinstance(value, list):
            value = self._deserialize(value)
        return value

    def get_prep_value(self, value):
        if not isinstance(value, dict):
            raise TypeError('ConsumableItemsField value should be dict. Received: %s' % value)
        if any([not isinstance(item, ConsumableItem) for item in value]):
            raise TypeError('ConsumableItemsField keys should be instances of ConsumableItem class.')

        return super(ConsumableItemsField, self).get_prep_value(self._serialize(value))

    def _serialize(self, value):
        return [{'usage': usage, 'item_type': item.item_type, 'key': item.key}
                for item, usage in value.items()]

    def _deserialize(self, serialized_value):
        return {ConsumableItem(item['item_type'], item['key']): item['usage'] for item in serialized_value}


class ConsumptionDetails(core_models.UuidMixin, TimeStampedModel):
    """ Resource consumption details per month.

        Warning! Use method "update_configuration" to update configurations,
        do not update them manually.
    """
    price_estimate = models.OneToOneField(PriceEstimate, related_name='consumption_details')
    configuration = ConsumableItemsField(default=dict, help_text=_('Current resource configuration.'))
    last_update_time = models.DateTimeField(help_text=_('Last configuration change time.'))
    consumed_before_update = ConsumableItemsField(
        default=dict, help_text=_('How many consumables were used by resource before last update.'))

    objects = managers.ConsumptionDetailsManager()

    class Meta:
        verbose_name = _('Consumption details')
        verbose_name_plural = _('Consumption details')

    def update_configuration(self, new_configuration):
        """ Save how much consumables were used and update current configuration.

            Return True if configuration changed.
        """
        if new_configuration == self.configuration:
            return False
        now = timezone.now()
        if now.month != self.price_estimate.month:
            raise ConsumptionDetailUpdateError('It is possible to update consumption details only for current month.')
        minutes_from_last_update = self._get_minutes_from_last_update(now)
        for consumable_item, usage in self.configuration.items():
            consumed_after_modification = usage * minutes_from_last_update
            self.consumed_before_update[consumable_item] = (
                self.consumed_before_update.get(consumable_item, 0) + consumed_after_modification)
        self.configuration = new_configuration
        self.last_update_time = now
        self.save()
        return True

    @property
    def consumed_in_month(self):
        """ How many resources were (or will be) consumed until end of the month """
        month_end = core_utils.month_end(datetime.date(self.price_estimate.year, self.price_estimate.month, 1))
        return self._get_consumed(month_end)

    @property
    def consumed_until_now(self):
        """ How many consumables were used by resource until now. """
        return self._get_consumed(timezone.now())

    def _get_consumed(self, time):
        """ How many consumables were (or will be) used by resource until given time. """
        minutes_from_last_update = self._get_minutes_from_last_update(time)
        if minutes_from_last_update < 0:
            raise ConsumptionDetailCalculateError('Cannot calculate consumption if time < last modification date.')
        _consumed = {}
        for consumable_item in set(list(self.configuration.keys()) + list(self.consumed_before_update.keys())):
            after_update = self.configuration.get(consumable_item, 0) * minutes_from_last_update
            before_update = self.consumed_before_update.get(consumable_item, 0)
            _consumed[consumable_item] = after_update + before_update
        return _consumed

    def _get_minutes_from_last_update(self, time):
        """ How much minutes passed from last update to given time """
        time_from_last_update = time - self.last_update_time
        return int(time_from_last_update.total_seconds() / 60)


class AbstractPriceListItem(models.Model):
    class Meta:
        abstract = True

    value = models.DecimalField("Hourly rate", default=0, max_digits=13, decimal_places=7)
    units = models.CharField(max_length=255, blank=True)

    @property
    def monthly_rate(self):
        return '%0.2f' % (self.value * core_utils.hours_in_month())

    @property
    def minute_rate(self):
        return float(self.value) / 60


@python_2_unicode_compatible
class DefaultPriceListItem(core_models.UuidMixin, core_models.NameMixin, AbstractPriceListItem):
    """
    Default price list item for all resources of supported service types.

    It is fetched from cost tracking backend.
    Field "name" represents how price item will be represented for user.
    """
    item_type = models.CharField(max_length=255, help_text=_('Type of price list item. Examples: storage, flavor.'))
    key = models.CharField(
        max_length=255, help_text=_('Key that corresponds particular consumable. Example: name of flavor.'))
    resource_content_type = models.ForeignKey(ContentType, default=None)
    tracker = FieldTracker()

    def __str__(self):
        return 'Price list item %s: %s = %s for %s' % (self.name, self.key, self.value, self.resource_content_type)

    class Meta:
        unique_together = ('key', 'item_type', 'resource_content_type')

    @property
    def resource_type(self):
        cls = self.resource_content_type.model_class()
        if cls:
            return SupportedServices.get_name_for_model(cls)

    @classmethod
    def get_consumable_items_pretty_names(cls, resource_content_type, consumable_items):
        query = Q()
        for consumable_item in consumable_items:
            query |= (Q(item_type=consumable_item.item_type) & Q(key=consumable_item.key))
        price_list_items = cls.objects.filter(query, resource_content_type=resource_content_type)
        return {ConsumableItem(item_type, key): name
                for item_type, key, name in price_list_items.values_list('item_type', 'key', 'name')}

    @classmethod
    def init_from_registered_resources(cls):
        created_items = []
        with transaction.atomic():
            for resource_class in CostTrackingRegister.registered_resources:
                resource_content_type = ContentType.objects.get_for_model(resource_class)
                for consumable_item in CostTrackingRegister.get_consumable_items(resource_class):
                    price_list_item, created = cls._create_or_update_default_price_list_item(
                        resource_content_type, consumable_item)
                    if created:
                        created_items.append(price_list_item)
        return created_items

    @classmethod
    def _create_or_update_default_price_list_item(cls, resource_content_type, consumable_item):
        default_item, created = DefaultPriceListItem.objects.update_or_create(
            resource_content_type=resource_content_type,
            item_type=consumable_item.item_type,
            key=consumable_item.key,
            defaults={'units': consumable_item.units},
        )
        if created:
            default_item.value = consumable_item.default_price
            default_item.name = consumable_item.name
            default_item.save()
        return default_item, created


class PriceListItem(core_models.UuidMixin, AbstractPriceListItem):
    """
    Price list item related to private service.
    It is entered manually by customer owner.
    """
    # Generic key to service
    content_type = models.ForeignKey(ContentType)
    object_id = models.PositiveIntegerField()
    service = GenericForeignKey('content_type', 'object_id')
    objects = managers.PriceListItemManager('service')
    default_price_list_item = models.ForeignKey(DefaultPriceListItem)

    class Meta:
        unique_together = ('content_type', 'object_id', 'default_price_list_item')

    @property
    def resource_type(self):
        return self.default_price_list_item.resource_type

    @property
    def key(self):
        return self.default_price_list_item.key

    @property
    def item_type(self):
        return self.default_price_list_item.item_type

    def clean(self):
        if not self.service:
            raise ValidationError(_('Service is not defined.'))

        if SupportedServices.is_public_service(self.service):
            raise ValidationError(_('Public service does not support price list items.'))

        resource = self.default_price_list_item.resource_content_type.model_class()
        valid_resources = SupportedServices.get_related_models(self.service)['resources']

        if resource not in valid_resources:
            raise ValidationError(_('Service does not support required content type.'))

    @staticmethod
    def get_for_resource(resource):
        """ Get list of all price list items that should be used for resource.

            If price list item is defined for service - return it, otherwise -
            return default price list item.
        """
        resource_content_type = ContentType.objects.get_for_model(resource)
        default_items = set(DefaultPriceListItem.objects.filter(resource_content_type=resource_content_type))
        service = resource.service_project_link.service
        items = set(PriceListItem.objects.filter(
            default_price_list_item__in=default_items, service=service).select_related('default_price_list_item'))
        rewrited_defaults = set([i.default_price_list_item for i in items])
        return items | (default_items - rewrited_defaults)
