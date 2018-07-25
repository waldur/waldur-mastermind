"""
Cost tracking - add-on for NC plugins.
Allows to calculate price estimates for resources from your plugin.

Check developer guide for more details.
"""
import logging

from django.utils.module_loading import autodiscover_modules

default_app_config = 'waldur_core.cost_tracking.apps.CostTrackingConfig'
logger = logging.getLogger(__name__)


class ConsumableItem(object):

    def __init__(self, item_type, key, name=None, units='', default_price=0):
        self.item_type = item_type
        self.key = key
        self.default_price = default_price
        self.name = name if name is not None else '%s: %s' % (item_type, key)
        self.units = units

    def __repr__(self):
        return 'ConsumableItem(%s)' % self.name

    def __str__(self):
        return self.name

    def __hash__(self):
        return hash((self.item_type, self.key))

    def __eq__(self, other):
        return (self.item_type, self.key) == (other.item_type, other.key)

    def __ne__(self, other):
        # Not strictly necessary, but to avoid having both x==y and x!=y True at the same time
        return not(self == other)


class CostTrackingStrategy(object):
    """ Describes all methods that should be implemented to enable cost
        tracking for particular resource.
    """
    resource_class = NotImplemented

    @classmethod
    def get_configuration(cls, resource):
        """ Return dictionary of consumables that are used by resource.

            Dictionary key - ConsumableItem instance.
            Dictionary value - how many units of consumable is used by resource.
            Example: {
                ConsumableItem('storage', '1 MB'): 10240,
                ConsumableItem('flavor', 'small'): 1,
                ...
            }
        """
        return {}

    @classmethod
    def get_consumable_items(cls):
        """ Return list of all possible consumed items.

            Output format:
            [
                ConsumableItem(
                    item_type=<type of consumable>,
                    key=<consumable name>,
                    name=<item pretty name, that will be visible for user>,
                    units=<consumable units (MB, GB, points, etc.>,
                    default_price=<price for consumable usage per hour>,
                )
                ...
            ]
            Output example:
            [
                ConsumableItem(
                    item_type="storage"
                    key="1 MB",
                    units="MB",
                    name="1 MB of storage",
                    default_price=0.5,
                ),
                ConsumableItem(
                    item_type="flavor"
                    key="small",
                    name="Small flavor",
                ),
                ...
            ]
        """
        return []


class ResourceNotRegisteredError(TypeError):
    pass


class CostTrackingRegister(object):
    """ Register of all connected NC plugins """
    registered_resources = {}
    is_autodiscovered = False

    @classmethod
    def autodiscover(cls):
        if not cls.is_autodiscovered:
            autodiscover_modules('cost_tracking')
            cls.is_autodiscovered = True

    @classmethod
    def register_strategy(cls, strategy):
        cls.registered_resources[strategy.resource_class] = strategy

    @classmethod
    def _get_strategy(cls, resource_class):
        try:
            return cls.registered_resources[resource_class]
        except KeyError:
            raise ResourceNotRegisteredError('Resource %s is not registered for cost tracking. Make sure that its '
                                             'strategy is added to CostTrackingRegister' % resource_class.__name__)

    @classmethod
    def get_configuration(cls, resource):
        """ Return how much consumables are used by resource with current configuration.

            Output example:
            {
                <ConsumableItem instance>: <usage>,
                <ConsumableItem instance>: <usage>,
                ...
            }
        """
        strategy = cls._get_strategy(resource.__class__)
        return strategy.get_configuration(resource)

    @classmethod
    def get_consumable_items(cls, resource_class):
        """ Get all possible consumable items for given resource class """
        strategy = cls._get_strategy(resource_class)
        return strategy.get_consumable_items()
