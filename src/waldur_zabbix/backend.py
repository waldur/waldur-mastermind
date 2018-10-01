from datetime import date, timedelta
from decimal import Decimal
import logging
import sys
import warnings

from django.conf import settings as django_settings
from django.db import connections, DatabaseError
from django.utils import six, timezone
import pyzabbix
import requests
from requests.exceptions import RequestException
from requests.packages.urllib3 import exceptions

from waldur_core.core.utils import datetime_to_timestamp, timestamp_to_datetime
from waldur_core.structure import ServiceBackend, ServiceBackendError, log_backend_action
from waldur_core.structure.utils import update_pulled_fields

from . import models, utils


logger = logging.getLogger(__name__)
sms_settings = getattr(django_settings, 'WALDUR_ZABBIX', {}).get('SMS_SETTINGS', {})


class ZabbixLogsFilter(logging.Filter):
    def filter(self, record):
        # Mute useless Zabbix log concerning JSON-RPC server endpoint.
        if record.getMessage().startswith('JSON-RPC Server Endpoint'):
            return False

        return super(ZabbixLogsFilter, self).filter(record)


pyzabbix.logger.addFilter(ZabbixLogsFilter())


class ZabbixBackendError(ServiceBackendError):
    pass


def reraise(exc):
    """
    Reraise ZabbixBackendError while maintaining traceback.
    """
    six.reraise(ZabbixBackendError, exc, sys.exc_info()[2])


class QuietSession(requests.Session):
    """Session class that suppresses warning about unsafe TLS sessions and clogging the logs.
    Inspired by: https://github.com/kennethreitz/requests/issues/2214#issuecomment-110366218
    """
    def request(self, *args, **kwargs):
        if not kwargs.get('verify', self.verify):
            with warnings.catch_warnings():
                if hasattr(exceptions, 'InsecurePlatformWarning'):  # urllib3 1.10 and lower does not have this warning
                    warnings.simplefilter('ignore', exceptions.InsecurePlatformWarning)
                warnings.simplefilter('ignore', exceptions.InsecureRequestWarning)
                return super(QuietSession, self).request(*args, **kwargs)
        else:
            return super(QuietSession, self).request(*args, **kwargs)


class ZabbixBackend(ServiceBackend):

    DEFAULTS = {
        'host_group_name': 'waldur',
        'templates_names': [],
        'database_parameters': {
            'host': 'localhost',
            'port': '3306',
            'name': 'zabbix',
            'user': 'admin',
            'password': '',
        },
        'interface_parameters': {
            'dns': '',
            'ip': '0.0.0.0',  # nosec
            'main': 1,
            'port': '10050',
            'type': 1,
            'useip': 1,
        },
        'sms_email_from': sms_settings.get('SMS_EMAIL_FROM'),
        'sms_email_rcpt': sms_settings.get('SMS_EMAIL_RCPT'),
    }

    TREND_DELAY_SECONDS = 60 * 60  # One hour
    HISTORY_DELAY_SECONDS = 15 * 60

    def __init__(self, settings):
        self.settings = settings

    @property
    def host_group_name(self):
        return self.settings.get_option('host_group_name')

    @property
    def templates_names(self):
        return self.settings.get_option('templates_names')

    @property
    def interface_parameters(self):
        return self.settings.get_option('interface_parameters')

    @property
    def database_parameters(self):
        return self.settings.get_option('database_parameters')

    @property
    def api(self):
        if not hasattr(self, '_api'):
            self._api = self._get_api(self.settings.backend_url,
                                      self.settings.username,
                                      self.settings.password)
        return self._api

    def ping(self, raise_exception=False):
        try:
            self.api.api_version()
        except Exception as e:
            if raise_exception:
                reraise(e)
            return False
        else:
            return True

    def sync(self):
        self._get_or_create_group_id(self.host_group_name)
        self.pull_templates()
        self.pull_user_groups()
        self.pull_users()
        for name in self.templates_names:
            if not models.Template.objects.filter(name=name).exists():
                raise ZabbixBackendError('Cannot find template with name "%s".' % name)
        if not self.interface_parameters:
            raise ZabbixBackendError('Interface parameters should not be empty.')

    @log_backend_action()
    def create_host(self, host):
        interface_parameters = host.interface_parameters or self.interface_parameters
        host_group_name = host.host_group_name or self.host_group_name

        templates_ids = [t.backend_id for t in host.templates.all()]
        group_id, _ = self._get_or_create_group_id(host_group_name)

        zabbix_host_id, created = self._get_or_create_host_id(
            host_name=host.name,
            visible_name=host.visible_name,
            group_id=group_id,
            templates_ids=templates_ids,
            interface_parameters=interface_parameters,
            status=host.status,
        )

        if not created:
            logger.warning('Host with name "%s" already exists', host.name)

        host.interface_parameters = interface_parameters
        host.host_group_name = host_group_name
        host.backend_id = zabbix_host_id
        host.save()

    @log_backend_action()
    def update_host(self, host):
        try:
            group_id, _ = self._get_or_create_group_id(host.host_group_name)
            self.api.host.update({
                'hostid': host.backend_id,
                'host': host.name,
                'name': host.visible_name,
                'group_id': group_id,
                'templates': [{'templateid': t.backend_id} for t in host.templates.all()],
                'status': host.status,
            })
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            reraise(e)

    @log_backend_action()
    def delete_host(self, host):
        try:
            self.api.host.delete(host.backend_id)
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            reraise(e)

    @log_backend_action()
    def create_itservice(self, itservice):
        name = itservice.name or self._get_service_name(itservice.host.scope.backend_id)

        try:
            creation_kwargs = {
                'name': name,
                'algorithm': itservice.algorithm,
                'showsla': self._get_showsla(itservice.algorithm),
                'sortorder': itservice.sort_order,
                'goodsla': six.text_type(itservice.agreed_sla),
                'triggerid': None,
            }
            if itservice.trigger and itservice.host:
                creation_kwargs['triggerid'] = self._get_trigger_id(itservice.host.backend_id, itservice.trigger.name)

            data = self.api.service.create(creation_kwargs)
            service_id = data['serviceids'][0]
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            reraise(e)
        except (IndexError, KeyError) as e:
            raise ZabbixBackendError('ITService create request returned unexpected response: %s', data)
        else:
            itservice.backend_id = service_id
            itservice.name = name

            data = self.get_itservice(itservice.backend_id)
            itservice.backend_trigger_id = data['triggerid']
            itservice.save(update_fields=['backend_id', 'name', 'backend_trigger_id'])

    @log_backend_action()
    def delete_itservice(self, itservice):
        try:
            self.api.service.delete(itservice.backend_id)
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            reraise(e)

    def pull_templates(self):
        """ Update existing Waldur templates and their items """
        logger.debug('About to pull zabbix templates from backend.')
        try:
            zabbix_templates = self.api.template.get(
                output=['name', 'templateid'],
                selectTriggers=['description', 'triggerid', 'priority'],
                selectItems=['itemid', 'name', 'key_', 'value_type', 'units', 'history', 'delay'],
                selectTemplates=['templateid'],
            )
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            raise ZabbixBackendError('Cannot pull templates. Exception: %s' % e)

        # Delete stale templates
        zabbix_templates_ids = set([t['templateid'] for t in zabbix_templates])
        models.Template.objects.filter(settings=self.settings).exclude(backend_id__in=zabbix_templates_ids).delete()

        # Update or create zabbix templates
        for zabbix_template in zabbix_templates:
            nc_template, created = models.Template.objects.get_or_create(
                backend_id=zabbix_template['templateid'],
                settings=self.settings,
                defaults={'name': zabbix_template['name']})
            if not created and nc_template.name != zabbix_template['name']:
                nc_template.name = zabbix_template['name']
                nc_template.save()

            # Delete stale triggers
            zabbix_triggers_ids = set([i['triggerid'] for i in zabbix_template['triggers']])
            nc_template.triggers.exclude(backend_id__in=zabbix_triggers_ids).delete()

            # Update or create triggers
            for zabbix_trigger in zabbix_template['triggers']:
                nc_template.triggers.update_or_create(
                    backend_id=zabbix_trigger['triggerid'],
                    priority=int(zabbix_trigger['priority']),  # according to Zabbix model it must always be integer
                    settings=nc_template.settings,
                    defaults={'name': zabbix_trigger['description']})

            # Delete stale items
            zabbix_items_ids = set([i['itemid'] for i in zabbix_template['items']])
            nc_template.items.exclude(backend_id__in=zabbix_items_ids).delete()

            # Update or create zabbix items
            for zabbix_item in zabbix_template['items']:
                defaults = {
                    'name': zabbix_item['name'],
                    'key': zabbix_item['key_'],
                    'value_type': int(zabbix_item['value_type']),
                    'units': zabbix_item['units'],
                    'history': utils.parse_time(zabbix_item['history']),
                    'delay': utils.parse_time(zabbix_item['delay'])
                }

                nc_item, created = nc_template.items.get_or_create(
                    backend_id=zabbix_item['itemid'], defaults=defaults)
                if not created:
                    update_fields = []
                    for (name, value) in defaults.items():
                        if getattr(nc_item, name) != value:
                            setattr(nc_item, name, value)
                            update_fields.append(name)
                    if update_fields:
                        nc_item.save(update_fields=update_fields)

        # Initialize templates children
        for zabbix_template in zabbix_templates:
            if not zabbix_template['templates']:
                continue
            nc_template = models.Template.objects.get(settings=self.settings, backend_id=zabbix_template['templateid'])
            children_ids = [t['templateid'] for t in zabbix_template['templates']]
            children = models.Template.objects.filter(settings=self.settings, backend_id__in=children_ids)
            nc_template.children.add(*children)

        logger.info('Successfully pulled Zabbix templates for settings %s', self.settings)

    def get_item_last_value(self, host_id, key, **kwargs):
        try:
            items = self.api.item.get(hostids=host_id, filter={'key_': 'application.status'}, output=['lastvalue'])
        except pyzabbix.ZabbixAPIException as e:
            raise ZabbixBackendError('Cannot get zabbix items. Exception: %s' % e)
        try:
            return items[0]['lastvalue']
        except IndexError:
            raise ZabbixBackendError('Cannot find item with key "%s" for host with id %s' % (key, host_id))

    # XXX: This method should be rewrited - we need to pull only IT services that were connected to hosts.
    def pull_itservices(self):
        """
        Update IT services
        """
        logger.debug('About to pull Zabbix IT services')

        try:
            zabbix_services = self.api.service.get(output='extend')
        except pyzabbix.ZabbixAPIException as e:
            raise ZabbixBackendError('Cannot pull IT services. Exception: %s' % e)

        # Delete stale services
        zabbix_services_ids = set(i['serviceid'] for i in zabbix_services)
        models.ITService.objects.filter(settings=self.settings).exclude(backend_id__in=zabbix_services_ids).delete()

        triggers_map = self._get_triggers_map(zabbix_services)

        for zabbix_service in zabbix_services:
            triggerid = zabbix_service['triggerid']
            nc_triggerid = triggers_map.get(triggerid)

            defaults = {
                'name': zabbix_service['name'],
                'algorithm': int(zabbix_service['algorithm']),
                'sort_order': int(zabbix_service['sortorder']),
                'agreed_sla': Decimal(zabbix_service['goodsla']),
                'backend_trigger_id': triggerid,
                'trigger_id': nc_triggerid
            }
            nc_service, created = models.ITService.objects.get_or_create(
                settings=self.settings,
                backend_id=zabbix_service['serviceid'],
                defaults=defaults
            )
            if not created:
                update_fields = []
                for (name, value) in defaults.items():
                    if getattr(nc_service, name) != value:
                        setattr(nc_service, name, value)
                        update_fields.append(name)
                if update_fields:
                    nc_service.save(update_fields=update_fields)

        logger.info('Successfully pulled Zabbix IT services for settings %s.', self.settings)

    def pull_user_groups(self):
        logger.debug('About to pull Zabbix user groups.')

        try:
            zabbix_user_groups = self.api.usergroup.get()
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            raise ZabbixBackendError('Cannot pull user groups. Exception: %s' % e)
        # Delete stale
        zabbix_user_group_ids = set(i['usrgrpid'] for i in zabbix_user_groups)
        models.UserGroup.objects.filter(settings=self.settings).exclude(backend_id__in=zabbix_user_group_ids).delete()
        # Update or create
        for zabbix_user_group in zabbix_user_groups:
            models.UserGroup.objects.update_or_create(
                backend_id=zabbix_user_group['usrgrpid'],
                settings=self.settings,
                defaults={'name': zabbix_user_group['name']})
        logger.info('Successfully pulled Zabbix user groups for settings %s.', self.settings)

    def pull_users(self):
        logger.debug('About to pull Zabbix users.')

        try:
            zabbix_users = self.api.user.get(selectUsrgrps=['name', 'usrgrpid'], output='extend')
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            raise ZabbixBackendError('Cannot pull users. Exception: %s' % e)
        # Delete stale
        zabbix_users_ids = set(i['userid'] for i in zabbix_users)
        models.User.objects.filter(settings=self.settings).exclude(backend_id__in=zabbix_users_ids).delete()
        # Update or create
        for zabbix_user in zabbix_users:
            groups = []
            for zabbix_user_group in zabbix_user['usrgrps']:
                group, _ = models.UserGroup.objects.get_or_create(
                    backend_id=zabbix_user_group['usrgrpid'],
                    settings=self.settings,
                    defaults={'name': zabbix_user_group['name']})
                groups.append(group)
            user, _ = models.User.objects.update_or_create(
                backend_id=zabbix_user['userid'],
                settings=self.settings,
                defaults={
                    'name': zabbix_user['name'],
                    'alias': zabbix_user['alias'],
                    'surname': zabbix_user['surname'],
                    'type': int(zabbix_user['type']),
                    'state': models.User.States.OK,
                })
            user.groups.add(*groups)

        logger.info('Successfully pulled Zabbix users for settings %s.', self.settings)

    @log_backend_action()
    def create_user(self, user):
        try:
            zabbix_user = self.api.user.create(
                name=user.name,
                surname=user.surname,
                alias=user.alias,
                type=user.type,
                passwd=user.password,
                usrgrps=[{'usrgrpid': group.backend_id for group in user.groups.all()}])
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            reraise(e)
        user.backend_id = zabbix_user['userids'][0]
        user.save(update_fields=['backend_id'])

    @log_backend_action()
    def update_user(self, user):
        try:
            self.api.user.update(
                userid=user.backend_id,
                name=user.name,
                surname=user.surname,
                alias=user.alias,
                type=user.type,
                passwd=user.password,
                usrgrps=[{'usrgrpid': group.backend_id for group in user.groups.all()}])
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            reraise(e)

    @log_backend_action()
    def delete_user(self, user):
        try:
            self.api.user.delete(user.backend_id)
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            reraise(e)

    # XXX: This method is hotfix for user group permissions management. We
    #      should create models for permissions. NC-1564.
    @log_backend_action()
    def add_permission_to_user_group(self, user_group, host_group_name, permission_id):
        try:
            host_group_id, _ = self._get_or_create_group_id(host_group_name)
            self.api.usergroup.update(
                usrgrpid=user_group.backend_id,
                rights=[{'id': host_group_id, 'permission': permission_id}]
            )
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            reraise(e)

    def _get_triggers_map(self, zabbix_services):
        """
        Return map of Zabbix trigger ID to Waldur trigger ID
        """
        trigger_ids = self._map_keys(zabbix_services, 'triggerid')

        zabbix_triggers = []
        try:
            zabbix_triggers = self.api.trigger.get(triggerids=trigger_ids, output=['triggerid', 'templateid'])
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            logger.exception('Unable to fetch Zabbix triggers')
            reraise(e)

        template_ids = self._map_keys(zabbix_triggers, 'templateid')
        nc_triggers = models.Trigger.objects.filter(
            settings=self.settings,
            backend_id__in=template_ids).only('pk')
        nc_triggers_map = {trigger.backend_id: trigger.pk for trigger in list(nc_triggers)}

        return {trigger['triggerid']: nc_triggers_map.get(trigger['templateid'])
                for trigger in zabbix_triggers}

    def _map_keys(self, items, key):
        return list(set(item[key] for item in items))

    def _get_or_create_group_id(self, group_name):
        try:
            exists = self.api.hostgroup.get(filter={'name': group_name})
            if not exists:
                group_id = self.api.hostgroup.create({'name': group_name})['groupids'][0]
                return group_id, True
            else:
                return self.api.hostgroup.get(filter={'name': group_name})[0]['groupid'], False
        except (pyzabbix.ZabbixAPIException, IndexError, KeyError) as e:
            raise ZabbixBackendError('Cannot get or create group with name "%s". Exception: %s' % (group_name, e))

    def _get_or_create_host_id(self, host_name, visible_name, group_id, templates_ids, interface_parameters, status):
        """ Create Zabbix host with given parameters.

        Return (<host_id>, <is_created>) tuple as result.
        """
        host_id = self._get_host_id(host_name)
        if host_id:
            return host_id, False
        host_id = self._create_host(host_name, visible_name, group_id, templates_ids,
                                    interface_parameters, status)
        return host_id, True

    def _get_host_id(self, host_name):
        try:
            host = self.api.host.get(filter={'host': host_name}, output='hostid')
            # If host with given name does not exist, empty list is returned
            if host:
                return host[0]['hostid']
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            logger.error('Cannot get host id by name: %s. Exception: %s', host_name, six.text_type(e))
            reraise(e)

    def _create_host(self, host_name, visible_name, group_id, templates_ids, interface_parameters, status):
        host_parameters = {
            "host": host_name,
            "name": visible_name,
            "interfaces": [interface_parameters],
            "groups": [{"groupid": group_id}],
            "templates": [{'templateid': template_id} for template_id in templates_ids],
            "status": status,
        }

        try:
            host = self.api.host.create(host_parameters)
            return host['hostids'][0]
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            logger.error('Cannot create host with parameters: %s. Exception: %s',
                         host_parameters, six.text_type(e))
            reraise(e)

    def _get_showsla(self, algorithm):
        if algorithm == models.ITService.Algorithm.ANY:
            return 1
        elif algorithm == models.ITService.Algorithm.ALL:
            return 1
        else:
            return 0

    def _get_service_name(self, backend_id):
        return 'Availability of %s' % backend_id

    def _get_trigger_id(self, host_id, description):
        """
        Find trigger ID by host ID and trigger description
        """
        try:
            data = self.api.trigger.get(
                filter={'description': description},
                hostids=host_id,
                output=['triggerid'])
            return data[0]['triggerid']
        except (pyzabbix.ZabbixAPIException, RequestException, IndexError, KeyError) as e:
            logger.exception('No trigger for host %s and description %s', host_id, description)
            reraise(e)

    def get_sla(self, service_id, start_time, end_time):
        try:
            data = self.api.service.getsla(
                filter={'serviceids': service_id},
                intervals={'from': start_time, 'to': end_time}
            )
            return data[service_id]['sla'][0]['sla']
        except (pyzabbix.ZabbixAPIException, RequestException, IndexError, KeyError) as e:
            message = 'Can not get Zabbix IT service SLA value for service with ID %s. Exception: %s'
            raise ZabbixBackendError(message % (service_id, e))

    def get_itservice(self, service_id):
        try:
            response = self.api.service.get(filter={'serviceid': service_id}, output='extend')
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            raise ZabbixBackendError('Can not get status of Zabbix IT service with ID %s. Exception: %s',
                                     service_id, e)
        if len(response) != 1:
            raise ZabbixBackendError('Zabbix IT service with ID %s is not found. '
                                     'Response is %s', service_id, response)
        return response[0]

    def get_sla_range(self, serviceid):
        """
        Execute query to Zabbix DB to get minimum and maximum clock for service's alarm.
        Returns minimum and maximum dates.
        """
        query = 'SELECT min(clock), max(clock) FROM service_alarms WHERE serviceid = %s'
        cursor = self._execute_query(query, [serviceid])
        min_timestamp, max_timestamp = cursor.fetchone()
        return date.fromtimestamp(int(min_timestamp)), date.fromtimestamp(int(max_timestamp))

    def get_trigger_events(self, trigger_id, start_time, end_time):
        try:
            event_data = self.api.event.get(
                output='extend',
                objectids=trigger_id,
                time_from=start_time,
                time_till=end_time,
                sortfield=["clock"],
                sortorder="ASC")
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            message = 'Can not get events for trigger with ID %s. Exception: %s'
            raise ZabbixBackendError(message % (trigger_id, e))
        else:
            return [{'timestamp': e['clock'], 'value': e['value']} for e in event_data]

    def _get_api(self, backend_url, username, password):
        unsafe_session = QuietSession()
        unsafe_session.verify = False

        api = pyzabbix.ZabbixAPI(server=backend_url, session=unsafe_session)
        api.login(username, password)
        return api

    def get_item_stats(self, hostid, item, points):
        if item.value_type == models.Item.ValueTypes.FLOAT:
            history_table = 'history'
            trend_table = 'trends'
        elif item.value_type == models.Item.ValueTypes.INTEGER:
            # Integer value
            history_table = 'history_uint'
            trend_table = 'trends_uint'
        else:
            raise ZabbixBackendError('Cannot get statistics for non-numerical item %s' % item.key)

        history_retention_days = item.history
        history_delay_seconds = item.delay or self.HISTORY_DELAY_SECONDS
        trend_delay_seconds = self.TREND_DELAY_SECONDS

        trends_start_date = datetime_to_timestamp(timezone.now() - timedelta(days=history_retention_days))

        points = points[::-1]
        history_cursor = self._get_history(
            item.key, hostid, history_table, points[-1] - history_delay_seconds, points[0])
        trends_cursor = self._get_history(
            item.key, hostid, trend_table, points[-1] - trend_delay_seconds, points[0])

        values = []
        if points[0] > trends_start_date:
            next_value = history_cursor.fetchone()
        else:
            next_value = trends_cursor.fetchone()

        for end, start in zip(points[:-1], points[1:]):
            if start > trends_start_date:
                interval = history_delay_seconds
            else:
                interval = trend_delay_seconds

            value = None
            while True:
                if next_value is None:
                    break
                time, value = next_value
                if item.is_byte():
                    value = self.b2mb(value)

                if time <= end:
                    if end - time < interval or time > start:
                        break
                else:
                    if start > trends_start_date:
                        next_value = history_cursor.fetchone()
                    else:
                        next_value = trends_cursor.fetchone()

            values.append(value)
        return values[::-1]

    def get_items_aggregated_values(self, host, items, start_timestamp, end_timestamp, method='MAX'):
        """
        Get aggregate values for host items.

        Output format:
            {
                <item1.name>: <aggregated value>,
                <item2.name>: <aggregated value>,
                ...
            }
        """
        int_items = [item for item in items if item.value_type == models.Item.ValueTypes.INTEGER]
        float_items = [item for item in items if item.value_type == models.Item.ValueTypes.FLOAT]

        # Get aggregated data from DB
        db_data = tuple()
        default_kwargs = {
            'hostid': host.backend_id,
            'start_timestamp': start_timestamp,
            'end_timestamp': end_timestamp,
            'method': method,
        }
        # XXX: We need to get values from table "trends" if end_timestamp < item.history.
        if int_items:
            cursor = self._get_aggregated_values(
                item_keys=[item.key for item in int_items],
                table='history_uint',
                **default_kwargs
            )
            db_data += cursor.fetchall()
        if float_items:
            cursor = self._get_aggregated_values(
                item_keys=[item.key for item in float_items],
                table='history',
                **default_kwargs
            )
            db_data += cursor.fetchall()

        # Prepare data - convert B to MB if needed
        items_keys = {item.key: item for item in items}
        aggregated_values = {}
        for key, value in db_data:
            item = items_keys[key]
            aggregated_values[key] = self.b2mb(value) if item.is_byte() else value
        return aggregated_values

    def b2mb(self, value):
        return value / 1024 / 1024

    def _get_history(self, item_key, hostid, table, start_timestamp, end_timestamp):
        """
        Execute query to zabbix db to get item values from history
        """
        query = (
            'SELECT clock time, %(value_path)s value '
            'FROM %(table)s history, items '
            'WHERE history.itemid = items.itemid '
            'AND items.key_ = "%(item_key)s" '
            'AND items.hostid = %(hostid)s '
            'AND clock > %(start_timestamp)s '
            'AND clock < %(end_timestamp)s '
            'ORDER BY clock DESC'
        )
        parameters = {
            'table': table,
            'item_key': item_key,
            'hostid': hostid,
            'start_timestamp': start_timestamp,
            'end_timestamp': end_timestamp,
            'value_path': table.startswith('history') and 'value' or 'value_avg'
        }
        query = query % parameters

        return self._execute_query(query)

    def _get_aggregated_values(self, hostid, item_keys, start_timestamp, end_timestamp, table, method='MAX'):
        """
        Execute query to zabbix DB to get item aggregated historical value.
        """
        # XXX: This query is really slow with a lot of item_keys, need to speed up it with index.
        query = (
            'SELECT items.key_, %(method)s(value) '
            'FROM hosts, items, %(table)s history '
            'WHERE items.hostid = %(hostid)s AND hosts.hostid = %(hostid)s AND history.itemid = items.itemid '
            'AND items.key_ IN (%(item_keys)s) '
            'AND clock >= %(start_timestamp)s '
            'AND clock <= %(end_timestamp)s '
            'GROUP BY items.key_'
        )

        parameters = {
            'method': method,
            'table': table,
            'hostid': hostid,
            'item_keys': ', '.join(['"%s"' % key for key in item_keys]),
            'start_timestamp': start_timestamp,
            'end_timestamp': end_timestamp,
        }

        query = query % parameters
        return self._execute_query(query)

    def _get_db_connection(self, force=False):
        host = self.database_parameters['host']
        port = self.database_parameters['port']
        name = self.database_parameters['name']
        user = self.database_parameters['user']
        password = self.database_parameters['password']

        key = '/'.join([name, host, port])
        if key not in connections.databases or force:
            connections.databases[key] = {
                'ENGINE': 'django.db.backends.mysql',
                'NAME': name,
                'HOST': host,
                'PORT': port,
                'USER': user,
                'PASSWORD': password
            }
        return connections[key]

    def _execute_query(self, query, *args, **kwargs):
        logger.debug('Executing query %s to Zabbix' % query)
        try:
            cursor = self._get_db_connection().cursor()
            cursor.execute(query, *args, **kwargs)
            return cursor
        except DatabaseError as e:
            logger.exception('Can not execute query the Zabbix DB.')
            reraise(e)

    def import_host(self, host_backend_id, service_project_link=None, save=True):
        if save and not service_project_link:
            raise AttributeError('Cannot save imported host if SPL is not defined.')
        try:
            backend_host = self.api.host.get(
                filter={'hostid': host_backend_id}, selectGroups=True,
                output=['host', 'name', 'description', 'error', 'status', 'groups'])[0]
        except IndexError:
            raise ZabbixBackendError('Host with id %s does not exist at backend' % host_backend_id)
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            reraise(e)

        host = models.Host()
        host.name = backend_host['host']
        host.visible_name = backend_host['name']
        host.description = backend_host['description']
        host.error = backend_host['error']
        host.status = backend_host['status']
        host.backend_id = host_backend_id
        if backend_host.get('groups'):
            # Host groups list is serialized as in following example:
            # [{u'internal': u'0', u'flags': u'0', u'groupid': u'15', u'name': u'waldur'}]
            host.host_group_name = backend_host['groups'][0]['name']
        else:
            host.host_group_name = ''

        if save:
            host.service_project_link = service_project_link
            host.save()
            templates = self.get_host_templates(host)
            host.templates.add(*templates)
        return host

    def get_host_templates(self, host):
        try:
            backend_templates = self.api.template.get(hostids=[host.backend_id], output=['templateid'])
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            reraise(e)
        return models.Template.objects.filter(
            backend_id__in=[t['templateid'] for t in backend_templates],
            settings=host.service_project_link.service.settings)

    @log_backend_action()
    def pull_host(self, host):
        import_time = timezone.now()
        imported_host = self.import_host(host.backend_id, save=False)
        imported_host_templates = set(self.get_host_templates(host))

        host.refresh_from_db()
        if host.modified < import_time:
            update_fields = ('name', 'visible_name', 'description', 'error', 'status', 'host_group_name')
            update_pulled_fields(host, imported_host, update_fields)

        host_templates = set(host.templates.all())
        host.templates.remove(*(host_templates - imported_host_templates))
        host.templates.add(*(imported_host_templates - host_templates))

    def get_trigger_request(self, query):
        request = {}

        request['selectHosts'] = 1
        request['active'] = 1
        request['expandComment'] = 1
        request['expandDescription'] = 1
        request['expandExpression'] = 1

        if 'host_id' in query:
            request['hostids'] = query['host_id']

        if 'host_name' in query:
            request['host'] = query['host_name']

        if 'changed_after' in query:
            request['lastChangeSince'] = datetime_to_timestamp(query['changed_after'])

        if 'changed_before' in query:
            request['lastChangeTill'] = datetime_to_timestamp(query['changed_before'])

        if 'min_priority' in query:
            request['min_severity'] = query['min_priority']

        if 'priority' in query and len(query['priority']) > 0:
            request['filter'] = {'priority': list(query['priority'])}

        if 'acknowledge_status' in query:
            acknowledge_status = query['acknowledge_status']
            Status = models.Trigger.AcknowledgeStatus
            status_mapping = {
                Status.SOME_EVENTS_UNACKNOWLEDGED: 'withUnacknowledgedEvents',
                Status.LAST_EVENT_UNACKNOWLEDGED: 'withLastEventUnacknowledged',
                Status.ALL_EVENTS_ACKNOWLEDGED: 'withAcknowledgedEvents',
            }
            if acknowledge_status in status_mapping:
                key = status_mapping[acknowledge_status]
                request[key] = 1

        if 'value' in query:
            request.setdefault('filter', {})
            request['filter']['value'] = query['value']

        return request

    def get_trigger_status(self, query):
        request = self.get_trigger_request(query)
        get_events_count = query.get('include_events_count')
        get_trigger_hosts = query.get('include_trigger_hosts')
        try:
            backend_triggers = self.api.trigger.get(**request)

            backend_events = None
            if get_events_count:
                objectids = map(lambda t: t['triggerid'], backend_triggers)
                backend_events = self.api.event.get(objectids=objectids,
                                                    acknowledged=0,
                                                    countOutput=True,
                                                    groupCount=True,
                                                    value='1')  # 1 means that trigger has a problem
                # https://www.zabbix.com/documentation/3.4/manual/api/reference/event/object

            trigger_hosts = None
            if get_trigger_hosts:
                objectids = map(lambda t: t['triggerid'], backend_triggers)
                trigger_hosts = self.api.host.get(triggerids=objectids)

            triggers = []

            for trigger in backend_triggers:
                triggers.append(self._parse_trigger(trigger, backend_events, trigger_hosts))

            return triggers
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            logger.exception('Unable to fetch Zabbix triggers')
            reraise(e)

    def _parse_trigger(self, backend_trigger, backend_events=None, trigger_hosts=None):
        trigger = {}

        for field in django_settings.WALDUR_ZABBIX['TRIGGER_FIELDS']:
            trigger[field[0]] = backend_trigger[field[1]]

        trigger['changed'] = timestamp_to_datetime(backend_trigger['lastchange'])
        trigger['hosts'] = []

        for host in backend_trigger['hosts']:
            host_id = host['hostid']

            if trigger_hosts is not None:
                host_name = filter(lambda h: h['hostid'] == host_id, trigger_hosts)
                if host_name:
                    host_name = host_name[0]['host']
                else:
                    host_name = ''
            else:
                host_name = None

            trigger['hosts'].append({'id': host_id, 'name': host_name})

        trigger['event_count'] = None
        if backend_events is not None:
            events = filter(lambda e: e['objectid'] == trigger['backend_id'], backend_events)
            trigger['event_count'] = 0 if not events else events[0]['rowscount']

        return trigger

    def get_trigger_count(self, query):
        request = self.get_trigger_request(query)
        try:
            return int(self.api.trigger.get(countOutput=True, **request))
        except (pyzabbix.ZabbixAPIException, RequestException) as e:
            logger.exception('Unable to fetch Zabbix triggers')
            reraise(e)
