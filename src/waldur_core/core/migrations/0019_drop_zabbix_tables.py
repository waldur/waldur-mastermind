from django.db import migrations

TABLES = (
    'monitoring_resourceitem',
    'monitoring_resourcesla',
    'monitoring_resourceslastatetransition',
    'waldur_zabbix_usergroup',
    'waldur_zabbix_item',
    'waldur_zabbix_trigger',
    'waldur_zabbix_host_templates',
    'waldur_zabbix_template',
    'waldur_zabbix_template_parents',
    'waldur_zabbix_host',
    'waldur_zabbix_zabbixserviceprojectlink',
    'waldur_zabbix_slahistory',
    'waldur_zabbix_user',
    'waldur_zabbix_user_groups',
    'waldur_zabbix_zabbixservice',
    'waldur_zabbix_itservice',
    'waldur_zabbix_slahistoryevent',
)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0018_drop_leftover_tables'),
    ]

    operations = [
        migrations.RunSQL(f'DROP TABLE IF EXISTS {table} CASCADE') for table in TABLES
    ]
