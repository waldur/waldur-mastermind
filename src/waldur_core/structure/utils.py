import collections
import logging

from django.db import models
from django.db.migrations.topological_sort import stable_topological_sort
from django.utils.lru_cache import lru_cache
import requests

from . import SupportedServices

logger = logging.getLogger(__name__)
Coordinates = collections.namedtuple('Coordinates', ('latitude', 'longitude'))
FieldInfo = collections.namedtuple('FieldInfo', 'fields fields_required extra_fields_required')


class GeoIpException(Exception):
    pass


def get_coordinates_by_ip(ip_address):
    url = 'http://freegeoip.net/json/{}'.format(ip_address)

    try:
        response = requests.get(url)
    except requests.exceptions.RequestException as e:
        raise GeoIpException("Request to geoip API %s failed: %s" % (url, e))

    if response.ok:
        data = response.json()
        return Coordinates(latitude=data['latitude'],
                           longitude=data['longitude'])
    else:
        params = (url, response.status_code, response.text)
        raise GeoIpException("Request to geoip API %s failed: %s %s" % params)


@lru_cache(maxsize=1)
def get_sorted_dependencies(service_model):
    """
    Returns list of application models in topological order.
    It is used in order to correctly delete dependent resources.
    """
    app_models = list(service_model._meta.app_config.get_models())
    dependencies = {model: set() for model in app_models}
    relations = (
        relation
        for model in app_models
        for relation in model._meta.related_objects
        if relation.on_delete in (models.PROTECT, models.CASCADE)
    )
    for rel in relations:
        dependencies[rel.model].add(rel.related_model)
    return stable_topological_sort(app_models, dependencies)


def sort_dependencies(service_model, resources):
    ordering = get_sorted_dependencies(service_model)
    resources.sort(key=lambda resource: ordering.index(resource._meta.model))
    return resources


@lru_cache(maxsize=1)
def get_all_services_field_info():
    services_fields = dict()
    services_fields_required = dict()
    services_extra_fields_required = dict()
    service_models = SupportedServices.get_service_models()

    for service_name in service_models:
        service_model = service_models[service_name]['service']
        service_serializer = SupportedServices.get_service_serializer(service_model)

        fields = service_serializer.SERVICE_ACCOUNT_FIELDS.keys() \
            if service_serializer.SERVICE_ACCOUNT_FIELDS is not NotImplemented else []

        fields_extra = service_serializer.SERVICE_ACCOUNT_EXTRA_FIELDS.keys() \
            if service_serializer.SERVICE_ACCOUNT_EXTRA_FIELDS is not NotImplemented else []

        fields_required = service_serializer.Meta.required_fields \
            if hasattr(service_serializer.Meta, 'required_fields') else []

        services_fields[service_name] = list(fields)
        services_fields_required[service_name] = list(set(fields) & set(fields_required))
        services_extra_fields_required[service_name] = list(set(fields_extra) & set(fields_required))

    return FieldInfo(fields=services_fields,
                     fields_required=services_fields_required,
                     extra_fields_required=services_extra_fields_required)


def update_pulled_fields(instance, imported_instance, fields):
    """
    Update instance fields based on imported from backend data.
    Save changes to DB only one or more fields were changed.
    """
    modified = False
    for field in fields:
        pulled_value = getattr(imported_instance, field)
        current_value = getattr(instance, field)
        if current_value != pulled_value:
            setattr(instance, field, pulled_value)
            logger.info("%s's with PK %s %s field updated from value '%s' to value '%s'",
                        instance.__class__.__name__, instance.pk, field, current_value, pulled_value)
            modified = True
    error_message = getattr(imported_instance, 'error_message', '') or getattr(instance, 'error_message', '')
    if error_message and instance.error_message != error_message:
        instance.error_message = imported_instance.error_message
        modified = True
    if modified:
        instance.save()


def handle_resource_not_found(resource):
    """
    Set resource state to ERRED and append/create "not found" error message.
    """
    resource.set_erred()
    resource.runtime_state = ''
    message = 'Does not exist at backend.'
    if message not in resource.error_message:
        if not resource.error_message:
            resource.error_message = message
        else:
            resource.error_message += ' (%s)' % message
    resource.save()
    logger.warning('%s %s (PK: %s) does not exist at backend.' % (
        resource.__class__.__name__, resource, resource.pk))


def handle_resource_update_success(resource):
    """
    Recover resource if its state is ERRED and clear error message.
    """
    update_fields = []
    if resource.state == resource.States.ERRED:
        resource.recover()
        update_fields.append('state')

    if resource.state in (resource.States.UPDATING, resource.States.CREATING):
        resource.set_ok()
        update_fields.append('state')

    if resource.error_message:
        resource.error_message = ''
        update_fields.append('error_message')

    if update_fields:
        resource.save(update_fields=update_fields)
    logger.warning('%s %s (PK: %s) was successfully updated.' % (
        resource.__class__.__name__, resource, resource.pk))
