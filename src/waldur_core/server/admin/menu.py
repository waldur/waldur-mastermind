from admin_tools.menu import items, Menu
from django.urls import reverse
from django.utils.text import capfirst
from django.utils.translation import ugettext_lazy as _

from waldur_core.core.utils import flatten


class CustomAppList(items.AppList):
    def init_with_context(self, context):
        context_items = self._visible_models(context['request'])
        apps = {}
        for model, perms in context_items:
            if not perms['change']:
                continue
            app_label = model._meta.app_label
            if app_label not in apps:
                apps[app_label] = {
                    'title': capfirst(model._meta.app_config.verbose_name),
                    'url': self._get_admin_app_list_url(model, context),
                    'models': []
                }
            apps[app_label]['models'].append({
                'title': capfirst(model._meta.verbose_name_plural),
                'url': self._get_admin_change_url(model, context)
            })

        for app in sorted(apps, key=lambda k: apps[k]['title']):
            app_dict = apps[app]
            item = items.MenuItem(title=app_dict['title'], url=app_dict['url'])
            # sort model list alphabetically
            apps[app]['models'].sort(key=lambda x: x['title'])
            for model_dict in apps[app]['models']:
                item.children.append(items.MenuItem(**model_dict))
            self.children.append(item)


class CustomMenu(Menu):
    """
    Custom Menu for admin site.
    """

    IAAS_CLOUDS = (
        'waldur_mastermind.packages.*',
        'waldur_azure.*',
        'waldur_openstack.*',
        'waldur_aws.*',
        'waldur_digitalocean.*',
        'waldur_slurm.*',
        'waldur_mastermind.slurm_invoices.*',
        'waldur_rijkscloud.*',
    )

    USERS = (
        'waldur_core.core.models.*',
        'waldur_core.users.models.*',
    )

    ACCOUNTING = (
        'waldur_mastermind.invoices.*',
        'waldur_core.cost_tracking.*',
        'waldur_paypal.*',
    )

    APPLICATION_PROVIDERS = (
        'waldur_ansible.*',
        'waldur_zabbix.*',
        'waldur_jira.*',
    )

    SUPPORT_MODULES = (
        'waldur_mastermind.support.*',
    )

    MARKETPLACE = (
        'waldur_mastermind.marketplace.*',
        'waldur_mastermind.marketplace_packages.*',
        'waldur_mastermind.marketplace_support.*',
    )

    EXTRA_MODELS = (
        'django.core.*',
        'django_openid_auth.*',
        'rest_framework.authtoken.*',
        'waldur_core.core.*',
        'waldur_core.structure.*',
    )

    def __init__(self, **kwargs):
        Menu.__init__(self, **kwargs)
        self.children += [
            items.MenuItem(_('Dashboard'), reverse('admin:index')),
            items.ModelList(
                _('Users'),
                models=self.USERS
            ),
            items.ModelList(
                _('Structure'),
                models=(
                    'waldur_core.structure.*',
                )
            ),
            CustomAppList(
                _('Accounting'),
                models=self.ACCOUNTING,
            ),
            CustomAppList(
                _('Marketplace'),
                models=self.MARKETPLACE,
            ),
            CustomAppList(
                _('Providers'),
                models=self.IAAS_CLOUDS,
            ),
            CustomAppList(
                _('Applications'),
                models=self.APPLICATION_PROVIDERS,
            ),
            CustomAppList(
                _('Support'),
                models=self.SUPPORT_MODULES,
            ),
            CustomAppList(
                _('Utilities'),
                exclude=flatten(
                    self.EXTRA_MODELS,
                    self.IAAS_CLOUDS,
                    self.APPLICATION_PROVIDERS,
                    self.SUPPORT_MODULES,
                    self.ACCOUNTING,
                    self.USERS,
                    self.MARKETPLACE,
                )
            ),
        ]
