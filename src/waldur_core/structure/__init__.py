from __future__ import unicode_literals

import functools
import importlib
import logging

from django.conf import settings
from django.utils.encoding import force_text
from django.utils.lru_cache import lru_cache
from rest_framework.reverse import reverse
import six

logger = logging.getLogger(__name__)


default_app_config = 'waldur_core.structure.apps.StructureConfig'


class SupportedServices(object):
    """ Comprehensive list of currently supported services and resources.
        Build the list via serializers definition on application start.
        Example data structure of registry:

        {
            'gitlab': {
                'name': 'GitLab',
                'model_name': 'gitlab.gitlabservice',
                'backend': nodeconductor_gitlab.backend.GitLabBackend,
                'detail_view': 'gitlab-detail',
                'list_view': 'gitlab-list',
                'properties': {},
                'resources': {
                    'gitlab.group': {
                        'name': 'Group',
                        'detail_view': 'gitlab-group-detail',
                        'list_view': 'gitlab-group-list'
                    },
                    'gitlab.project': {
                        'name': 'Project',
                        'detail_view': 'gitlab-project-detail',
                        'list_view': 'gitlab-project-list'
                    }
                }
            }
        }

    """

    @classmethod
    def get_filter_mapping(cls):
        return {name: code for code, name in cls.get_choices()}

    _registry = {}

    @classmethod
    def _setdefault(cls, service_key):
        cls._registry.setdefault(service_key, {
            'resources': {},
            'properties': {}
        })

    @classmethod
    def register_backend(cls, backend_class, nested=False):
        if not cls._is_active_model(backend_class):
            return

        # For nested backends just discover resources/properties
        if not nested:
            key = cls.get_model_key(backend_class)
            cls._setdefault(key)
            cls._registry[key]['backend'] = backend_class

        # Forcely import service serialize to run services autodiscovery
        try:
            module_name = backend_class.__module__
            importlib.import_module(module_name.replace('backend', 'serializers'))
        except ImportError:
            pass

    @classmethod
    def register_service(cls, model):
        if model is NotImplemented or not cls._is_active_model(model):
            return
        key = cls.get_model_key(model)
        cls._setdefault(key)
        cls._registry[key]['name'] = key
        cls._registry[key]['model_name'] = cls._get_model_str(model)
        cls._registry[key]['detail_view'] = cls.get_detail_view_for_model(model)
        cls._registry[key]['list_view'] = cls.get_list_view_for_model(model)

    @classmethod
    def register_service_serializer(cls, model, serializer):
        if model is NotImplemented or not cls._is_active_model(model):
            return
        key = cls.get_model_key(model)
        cls._setdefault(key)
        cls._registry[key]['serializer'] = serializer

    @classmethod
    def register_service_filter(cls, model, filter):
        if model is NotImplemented or not cls._is_active_model(model):
            return
        key = cls.get_model_key(model)
        cls._setdefault(key)
        cls._registry[key]['filter'] = filter

    @classmethod
    def register_resource_serializer(cls, model, serializer):
        if model is NotImplemented or not cls._is_active_model(model):
            return
        key = cls.get_model_key(model)
        cls._setdefault(key)
        model_str = cls._get_model_str(model)
        cls._registry[key]['resources'].setdefault(model_str, {'name': model.__name__})
        cls._registry[key]['resources'][model_str]['detail_view'] = cls.get_detail_view_for_model(model)
        cls._registry[key]['resources'][model_str]['list_view'] = cls.get_list_view_for_model(model)
        cls._registry[key]['resources'][model_str]['serializer'] = serializer

    @classmethod
    def register_resource_filter(cls, model, filter):
        if model is NotImplemented or not cls._is_active_model(model) or model._meta.abstract:
            return
        key = cls.get_model_key(model)
        cls._setdefault(key)
        model_str = cls._get_model_str(model)
        cls._registry[key]['resources'].setdefault(model_str, {'name': model.__name__})
        cls._registry[key]['resources'][model_str]['filter'] = filter

    @classmethod
    def register_resource_view(cls, model, view):
        if model is NotImplemented or not cls._is_active_model(model) or model._meta.abstract:
            return
        key = cls.get_model_key(model)
        cls._setdefault(key)
        model_str = cls._get_model_str(model)
        cls._registry[key]['resources'].setdefault(model_str, {'name': model.__name__})
        cls._registry[key]['resources'][model_str]['view'] = view

    @classmethod
    def register_property(cls, model):
        if model is NotImplemented or not cls._is_active_model(model):
            return
        key = cls.get_model_key(model)
        cls._setdefault(key)
        model_str = cls._get_model_str(model)
        cls._registry[key]['properties'][model_str] = {
            'name': model.__name__,
            'list_view': cls.get_list_view_for_model(model)
        }

    @classmethod
    def get_service_backend(cls, key):
        if not isinstance(key, six.string_types):
            key = cls.get_model_key(key)
        try:
            return cls._registry[key]['backend']
        except KeyError:
            raise ServiceBackendNotImplemented

    @classmethod
    def get_services(cls, request=None):
        """ Get a list of services endpoints.
            {
                "Oracle": "/api/oracle/",
                "OpenStack": "/api/openstack/",
                "GitLab": "/api/gitlab/",
                "DigitalOcean": "/api/digitalocean/"
            }
        """
        return {service['name']: reverse(service['list_view'], request=request)
                for service in cls._registry.values()}

    @classmethod
    def get_service_serializer(cls, model):
        key = cls.get_model_key(model)
        return cls._registry[key]['serializer']

    @classmethod
    def get_service_filter(cls, model):
        key = cls.get_model_key(model)
        return cls._registry[key]['filter']

    @classmethod
    def get_resources(cls, request=None):
        """ Get a list of resources endpoints.
            {
                "DigitalOcean.Droplet": "/api/digitalocean-droplets/",
                "Oracle.Database": "/api/oracle-databases/",
                "GitLab.Group": "/api/gitlab-groups/",
                "GitLab.Project": "/api/gitlab-projects/"
            }
        """
        return {'.'.join([service['name'], resource['name']]): reverse(resource['list_view'], request=request)
                for service in cls._registry.values()
                for resource in service['resources'].values()}

    @classmethod
    def get_resource_serializer(cls, model):
        key = cls.get_model_key(model)
        model_str = cls._get_model_str(model)
        return cls._registry[key]['resources'][model_str]['serializer']

    @classmethod
    def get_resource_filter(cls, model):
        key = cls.get_model_key(model)
        model_str = cls._get_model_str(model)
        return cls._registry[key]['resources'][model_str]['filter']

    @classmethod
    def get_resource_view(cls, model):
        key = cls.get_model_key(model)
        model_str = cls._get_model_str(model)
        return cls._registry[key]['resources'][model_str]['view']

    @classmethod
    def get_services_with_resources(cls, request=None):
        """ Get a list of services and resources endpoints.
            {
                ...
                "GitLab": {
                    "url": "/api/gitlab/",
                    "service_project_link_url": "/api/gitlab-service-project-link/",
                    "resources": {
                        "Project": "/api/gitlab-projects/",
                        "Group": "/api/gitlab-groups/"
                    }
                },
                ...
            }
        """
        from django.apps import apps

        data = {}
        for service in cls._registry.values():
            service_model = apps.get_model(service['model_name'])
            service_project_link = service_model.projects.through
            service_project_link_url = reverse(cls.get_list_view_for_model(service_project_link), request=request)

            data[service['name']] = {
                'url': reverse(service['list_view'], request=request),
                'service_project_link_url': service_project_link_url,
                'resources': {resource['name']: reverse(resource['list_view'], request=request)
                              for resource in service['resources'].values()},
                'properties': {resource['name']: reverse(resource['list_view'], request=request)
                               for resource in service.get('properties', {}).values()},
                'is_public_service': cls.is_public_service(service_model)
            }
        return data

    @classmethod
    @lru_cache(maxsize=1)
    def get_service_models(cls):
        """ Get a list of service models.
            {
                ...
                'gitlab': {
                    "service": nodeconductor_gitlab.models.GitLabService,
                    "service_project_link": nodeconductor_gitlab.models.GitLabServiceProjectLink,
                    "resources": [
                        nodeconductor_gitlab.models.Group,
                        nodeconductor_gitlab.models.Project
                    ],
                },
                ...
            }

        """
        from django.apps import apps

        data = {}
        for key, service in cls._registry.items():
            service_model = apps.get_model(service['model_name'])
            service_project_link = service_model.projects.through
            data[key] = {
                'service': service_model,
                'service_project_link': service_project_link,
                'resources': [apps.get_model(r) for r in service['resources'].keys()],
                'properties': [apps.get_model(r) for r in service['properties'].keys() if '.' in r],
            }

        return data

    @classmethod
    @lru_cache(maxsize=1)
    def get_resource_models(cls):
        """ Get a list of resource models.
            {
                'DigitalOcean.Droplet': waldur_digitalocean.models.Droplet,
                'JIRA.Project': waldur_jira.models.Project,
                'OpenStack.Tenant': waldur_openstack.models.Tenant
            }

        """
        from django.apps import apps

        return {'.'.join([service['name'], attrs['name']]): apps.get_model(resource)
                for service in cls._registry.values()
                for resource, attrs in service['resources'].items()}

    @classmethod
    @lru_cache(maxsize=20)
    def get_service_resources(cls, model):
        """ Get resource models by service model """
        key = cls.get_model_key(model)
        return cls.get_service_name_resources(key)

    @classmethod
    @lru_cache(maxsize=20)
    def get_resource_serializers(cls):
        return [resource['serializer']
                for provider in cls._registry.values()
                for resource in provider['resources'].values()]

    @classmethod
    @lru_cache(maxsize=20)
    def get_service_name_resources(cls, service_name):
        """ Get resource models by service name """
        from django.apps import apps

        resources = cls._registry[service_name]['resources'].keys()
        return [apps.get_model(resource) for resource in resources]

    @classmethod
    def get_name_for_model(cls, model):
        """ Get a name for given class or model:
            -- it's a service type for a service
            -- it's a <service_type>.<resource_model_name> for a resource
        """
        key = cls.get_model_key(model)
        model_str = cls._get_model_str(model)
        service = cls._registry[key]
        if model_str in service['resources']:
            return '{}.{}'.format(service['name'], service['resources'][model_str]['name'])
        else:
            return service['name']

    @classmethod
    def get_related_models(cls, model):
        """ Get a dictionary with related structure models for given class or model:

            >> SupportedServices.get_related_models(gitlab_models.Project)
            {
                'service': nodeconductor_gitlab.models.GitLabService,
                'service_project_link': nodeconductor_gitlab.models.GitLabServiceProjectLink,
                'resources': [
                    nodeconductor_gitlab.models.Group,
                    nodeconductor_gitlab.models.Project,
                ]
            }
        """
        from waldur_core.structure.models import ServiceSettings

        if isinstance(model, ServiceSettings):
            model_str = cls._registry.get(model.type, {}).get('model_name', '')
        else:
            model_str = cls._get_model_str(model)

        for models in cls.get_service_models().values():
            if model_str == cls._get_model_str(models['service']) or \
               model_str == cls._get_model_str(models['service_project_link']):
                return models

            for resource_model in models['resources']:
                if model_str == cls._get_model_str(resource_model):
                    return models

    @classmethod
    def _is_active_model(cls, model):
        """ Check is model app name is in list of INSTALLED_APPS """
        # We need to use such tricky way to check because of inconsistent apps names:
        # some apps are included in format "<module_name>.<app_name>" like "waldur_core.openstack"
        # other apps are included in format "<app_name>" like "nodecondcutor_sugarcrm"
        return ('.'.join(model.__module__.split('.')[:2]) in settings.INSTALLED_APPS or
                '.'.join(model.__module__.split('.')[:1]) in settings.INSTALLED_APPS)

    @classmethod
    def _get_model_str(cls, model):
        return force_text(model._meta)

    @classmethod
    def get_model_key(cls, model):
        return cls.get_app_config(model).service_name

    @classmethod
    def is_public_service(cls, model):
        return getattr(cls.get_app_config(model), 'is_public_service', False)

    @classmethod
    def get_app_config(cls, model):
        from django.apps import apps
        return apps.get_containing_app_config(model.__module__)

    @classmethod
    def get_list_view_for_model(cls, model):
        return model.get_url_name() + '-list'

    @classmethod
    def get_detail_view_for_model(cls, model):
        return model.get_url_name() + '-detail'

    @classmethod
    @lru_cache(maxsize=1)
    def get_choices(cls):
        items = [(code, service['name']) for code, service in cls._registry.items()]
        return sorted(items, key=lambda pair: pair[1])

    @classmethod
    def has_service_type(cls, service_type):
        return service_type in cls._registry

    @classmethod
    def get_name_for_type(cls, service_type):
        try:
            return cls._registry[service_type]['name']
        except KeyError:
            return service_type


class ServiceBackendError(Exception):
    """ Base exception for errors occurring during backend communication. """
    pass


def log_backend_action(action=None):
    """ Logging for backend method.

    Expects django model instance as first argument.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapped(self, instance, *args, **kwargs):
            action_name = func.func_name.replace('_', ' ') if action is None else action

            logger.debug('About to %s `%s` (PK: %s).', action_name, instance, instance.pk)
            result = func(self, instance, *args, **kwargs)
            logger.debug('Action `%s` was executed successfully for `%s` (PK: %s).',
                         action_name, instance, instance.pk)
            return result
        return wrapped
    return decorator


class ServiceBackendNotImplemented(NotImplementedError):
    pass


class ServiceBackend(object):
    """ Basic service backed with only common methods pre-defined. """

    DEFAULTS = {}

    def __init__(self, settings, **kwargs):
        pass

    def ping(self, raise_exception=False):
        raise ServiceBackendNotImplemented

    def ping_resource(self, resource):
        raise ServiceBackendNotImplemented

    def sync(self):
        raise ServiceBackendNotImplemented

    def has_global_properties(self):
        return False

    def provision(self, resource, *args, **kwargs):
        raise ServiceBackendNotImplemented

    def destroy(self, resource, force=False):
        raise ServiceBackendNotImplemented

    def stop(self, resource):
        raise ServiceBackendNotImplemented

    def start(self, resource):
        raise ServiceBackendNotImplemented

    def restart(self, resource):
        raise ServiceBackendNotImplemented

    def get_resources_for_import(self):
        raise ServiceBackendNotImplemented

    def get_managed_resources(self):
        raise ServiceBackendNotImplemented

    def get_monthly_cost_estimate(self, resource):
        raise ServiceBackendNotImplemented

    def get_stats(self):
        raise ServiceBackendNotImplemented

    @staticmethod
    def gb2mb(val):
        return int(val * 1024) if val else 0

    @staticmethod
    def tb2mb(val):
        return int(val * 1024 * 1024) if val else 0

    @staticmethod
    def mb2gb(val):
        return val / 1024 if val else 0

    @staticmethod
    def mb2tb(val):
        return val / 1024 / 1024 if val else 0
