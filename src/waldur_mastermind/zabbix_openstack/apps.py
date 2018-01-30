from django.apps import AppConfig


class ZabbixOpenStackConfig(AppConfig):
    name = 'waldur_mastermind.zabbix_openstack'
    verbose_name = 'Zabbix OpenStack'

    def ready(self):
        pass
