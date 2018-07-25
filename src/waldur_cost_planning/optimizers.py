""" This module calculates the cheapest price for deployment plans. """
import collections

from waldur_core.structure import models as structure_models

from . import register


def get_filtered_services(deployment_plan):
    """ Get services that fits deployment plan requirements """
    service_models = structure_models.Service.get_all_models()
    deployment_plan_certifications = deployment_plan.get_required_certifications()
    for model in service_models:
        services = (
            model.objects
            .filter(projects=deployment_plan.project)
            .select_related('settings')
            .prefetch_related('settings__certifications')
        )
        for service in services:
            if set(service.settings.certifications.all()).issuperset(deployment_plan_certifications):
                yield service


# http://stackoverflow.com/questions/11351032/named-tuple-and-optional-keyword-arguments
def namedtuple_with_defaults(typename, field_names, default_values=()):
    T = collections.namedtuple(typename, field_names)
    T.__new__.__defaults__ = (None,) * len(T._fields)
    if isinstance(default_values, collections.Mapping):
        prototype = T(**default_values)
    else:
        prototype = T(*default_values)
    T.__new__.__defaults__ = tuple(prototype)
    T._defaults = tuple(prototype)  # added for easier access
    return T


# Abstract object that represents the best choice for a particular service.
OptimizedService = namedtuple_with_defaults(
    'OptimizedService', ('service', 'price', 'error_message'), {'error_message': ''})


class Strategy(object):
    """ Abstract. Defines how get the cheapest services setups for deployment plan. """

    def __init__(self, deployment_plan):
        self.deployment_plan = deployment_plan

    def get_optimized(self):
        """ Return list of OptimizedService objects """
        raise NotImplementedError()


class SingleServiceStrategy(Strategy):
    """ Optimize deployment plan for each service separately and return list
        of all available variants.
    """

    def _get_optimized_service(self, service):
        optimizer_class = register.Register.get_optimizer(service.settings.type)
        if optimizer_class:
            optimizer = optimizer_class()
            try:
                return optimizer.optimize(self.deployment_plan, service)
            except OptimizationError as e:
                return OptimizedService(service=service, price=None, error_message=str(e))

    def get_optimized(self):
        optimized = []
        for service in get_filtered_services(self.deployment_plan):
            optimized_service = self._get_optimized_service(service)
            if optimized_service:
                optimized.append(optimized_service)
        return optimized


# Optimizer should raise this error if it is impossible to setup
# deployment plan for service
class OptimizationError(Exception):
    pass


class Optimizer(object):
    """ Abstract. Descendant should define how to get the cheapest setup for a
        particular service.
    """

    def optimize(self, deployment_plan, service):
        """ Return the cheapest setup as OptimizedService object """
        raise NotImplementedError()
