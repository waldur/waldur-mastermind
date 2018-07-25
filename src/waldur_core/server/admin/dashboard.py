from __future__ import unicode_literals

from django.apps import apps
from django.conf import settings
from django.urls import reverse
from django.utils.translation import ugettext_lazy as _
from fluent_dashboard.dashboard import modules, FluentIndexDashboard, FluentAppIndexDashboard
import six

from waldur_core import __version__
from waldur_core.core import models as core_models, WaldurExtension
from waldur_core.structure import models as structure_models, SupportedServices


class CustomIndexDashboard(FluentIndexDashboard):
    """
    Custom index dashboard for admin site.
    """
    title = _('Waldur administration')

    def _get_installed_plugin_info(self):
        links = []

        for ext in WaldurExtension.get_extensions():
            app_config = self._get_app_config(ext.django_app())
            if not app_config:
                # App is not found
                continue

            name = self._get_app_name(app_config)
            version = self._get_app_version(app_config)

            links.append(
                {
                    'title': '%s %s' % (name, version),
                    'url': 'http://docs.waldur.com/',
                    'external': True,
                    'attrs': {'target': '_blank'},
                }
            )

        # Order plugins by title
        links = sorted(links, key=lambda item: item['title'].lower())

        # Core is the most important component, therefore
        # it should be the the first item in the list
        links.insert(0, {
            'title': _('Waldur Core %s') % __version__,
            'url': 'http://docs.waldur.com/',
            'external': True,
            'attrs': {'target': '_blank'},
        })

        return links

    def _get_app_config(self, app_name):
        """
        Returns an app config for the given name, not by label.
        """

        matches = [app_config for app_config in apps.get_app_configs()
                   if app_config.name == app_name]
        if not matches:
            return
        return matches[0]

    def _get_app_name(self, app_config):
        """
        Strip redundant prefixes, because some apps
        don't specify prefix, while others use deprecated prefix.
        """

        return app_config.verbose_name\
            .replace('Waldur', '')\
            .strip()

    def _get_app_version(self, app_config):
        """
        Some plugins ship multiple applications and extensions.
        However all of them have the same version, because they are released together.
        That's why only-top level module is used to fetch version information.
        """

        base_name = app_config.__module__.split('.')[0]
        module = __import__(base_name)
        return getattr(module, '__version__', 'N/A')

    def _get_quick_access_info(self):
        """
        Returns a list of ListLink items to be added to Quick Access tab.
        Contains:
        - links to Organizations, Projects and Users;
        - a link to shared service settings;
        - custom configured links in admin/settings FLUENT_DASHBOARD_QUICK_ACCESS_LINKS attribute;
        """
        quick_access_links = []

        # add custom links
        quick_access_links.extend(settings.FLUENT_DASHBOARD_QUICK_ACCESS_LINKS)

        for model in (structure_models.Project,
                      structure_models.Customer,
                      core_models.User,
                      structure_models.SharedServiceSettings):
            quick_access_links.append(self._get_link_to_model(model))

        return quick_access_links

    def _get_erred_resource_link(self, model, erred_amount, erred_state):
        result = self._get_link_to_model(model)
        result['title'] = _('%(num)s %(resources)s in ERRED state') % {
            'num': erred_amount,
            'resources': result['title']
        }
        result['url'] = '%s?shared__exact=1&state__exact=%s' % (result['url'], erred_state)
        return result

    def _get_link_to_model(self, model):
        return {
            'title': six.text_type(model._meta.verbose_name_plural).capitalize(),
            'url': reverse('admin:%s_%s_changelist' % (model._meta.app_label, model._meta.model_name)),
            'external': True,
            'attrs': {'target': '_blank'},
        }

    def _get_link_to_instance(self, instance):
        return {
            'title': six.text_type(instance),
            'url': reverse('admin:%s_%s_change' % (instance._meta.app_label, instance._meta.model_name),
                           args=(instance.pk,)),
            'external': True,
            'attrs': {'target': '_blank'},
        }

    def _get_erred_shared_settings_module(self):
        """
        Returns a LinkList based module which contains link to shared service setting instances in ERRED state.
        """
        result_module = modules.LinkList(title=_('Shared provider settings in erred state'))
        result_module.template = 'admin/dashboard/erred_link_list.html'
        erred_state = structure_models.SharedServiceSettings.States.ERRED

        queryset = structure_models.SharedServiceSettings.objects
        settings_in_erred_state = queryset.filter(state=erred_state).count()

        if settings_in_erred_state:
            result_module.title = '%s (%s)' % (result_module.title, settings_in_erred_state)
            for service_settings in queryset.filter(state=erred_state).iterator():
                module_child = self._get_link_to_instance(service_settings)
                module_child['error'] = service_settings.error_message
                result_module.children.append(module_child)
        else:
            result_module.pre_content = _('Nothing found.')

        return result_module

    def _get_erred_resources_module(self):
        """
        Returns a list of links to resources which are in ERRED state and linked to a shared service settings.
        """
        result_module = modules.LinkList(title=_('Resources in erred state'))
        erred_state = structure_models.NewResource.States.ERRED
        children = []

        resource_models = SupportedServices.get_resource_models()
        resources_in_erred_state_overall = 0
        for resource_type, resource_model in resource_models.items():
            queryset = resource_model.objects.filter(service_project_link__service__settings__shared=True)
            erred_amount = queryset.filter(state=erred_state).count()
            if erred_amount:
                resources_in_erred_state_overall = resources_in_erred_state_overall + erred_amount
                link = self._get_erred_resource_link(resource_model, erred_amount, erred_state)
                children.append(link)

        if resources_in_erred_state_overall:
            result_module.title = '%s (%s)' % (result_module.title, resources_in_erred_state_overall)
            result_module.children = children
        else:
            result_module.pre_content = _('Nothing found.')

        return result_module

    def __init__(self, **kwargs):
        FluentIndexDashboard.__init__(self, **kwargs)

        self.children.append(modules.LinkList(
            _('Installed components'),
            layout='stacked',
            enabled=False,
            draggable=True,
            deletable=True,
            collapsible=True,
            children=self._get_installed_plugin_info()
        ))

        self.children.append(modules.LinkList(
            _('Quick access'),
            children=self._get_quick_access_info())
        )

        self.children.append(self._get_erred_shared_settings_module())
        self.children.append(self._get_erred_resources_module())


class CustomAppIndexDashboard(FluentAppIndexDashboard):
    def __init__(self, app_title, models, **kwargs):
        super(CustomAppIndexDashboard, self).__init__(app_title, models, **kwargs)
        path = self._get_app_models_path()
        self.children = [modules.ModelList(title=app_title, models=[path])]

    def _get_app_models_path(self):
        return '%s.models.*' % self.app_title.replace(' ', '.', 1).replace(' ', '_').lower()
