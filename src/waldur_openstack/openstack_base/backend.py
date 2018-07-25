import datetime
import hashlib
import logging

from ceilometerclient import client as ceilometer_client
from ceilometerclient import exc as ceilometer_exceptions
from cinderclient import exceptions as cinder_exceptions
from cinderclient.v2 import client as cinder_client
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from glanceclient import exc as glance_exceptions
from glanceclient.v2 import client as glance_client
from keystoneauth1 import session as keystone_session
from keystoneauth1.identity import v3
from keystoneclient import exceptions as keystone_exceptions
from keystoneclient.v3 import client as keystone_client
from neutronclient.client import exceptions as neutron_exceptions
from neutronclient.v2_0 import client as neutron_client
from novaclient import client as nova_client
from novaclient import exceptions as nova_exceptions
from requests import ConnectionError
import six

from waldur_core.structure import ServiceBackend
from waldur_core.structure.exceptions import SerializableBackendError
from waldur_openstack.openstack.models import Tenant

logger = logging.getLogger(__name__)


class OpenStackBackendError(SerializableBackendError):
    pass


class OpenStackSessionExpired(OpenStackBackendError):
    pass


class OpenStackAuthorizationFailed(OpenStackBackendError):
    pass


class OpenStackSession(dict):
    """ Serializable session """

    def __init__(self, ks_session=None, verify_ssl=False, **credentials):
        self.keystone_session = ks_session
        if not self.keystone_session:
            auth_plugin = v3.Password(**credentials)
            self.keystone_session = keystone_session.Session(auth=auth_plugin, verify=verify_ssl)

        try:
            # This will eagerly sign in throwing AuthorizationFailure on bad credentials
            self.keystone_session.get_auth_headers()
        except keystone_exceptions.ClientException as e:
            six.reraise(OpenStackAuthorizationFailed, e)

        for opt in ('auth_ref', 'auth_url', 'project_id', 'project_name', 'project_domain_name'):
            self[opt] = getattr(self.auth, opt)

    def __getattr__(self, name):
        return getattr(self.keystone_session, name)

    @classmethod
    def recover(cls, session, verify_ssl=False):
        if not isinstance(session, dict) or not session.get('auth_ref'):
            raise OpenStackBackendError('Invalid OpenStack session')

        args = {
            'auth_url': session['auth_url'],
            'token': session['auth_ref'].auth_token,
        }
        if session.get('project_id'):
            args['project_id'] = session['project_id']
        elif session.get('project_name') and session.get('project_domain_name'):
            args['project_name'] = session['project_name']
            args['project_domain_name'] = session['project_domain_name']

        ks_session = keystone_session.Session(auth=v3.Token(**args), verify=verify_ssl)
        return cls(ks_session=ks_session)

    def validate(self):
        if self.auth.auth_ref.expires > timezone.now() + datetime.timedelta(minutes=10):
            return True

        raise OpenStackSessionExpired('OpenStack session is expired')

    def __str__(self):
        return str({k: v if k != 'password' else '***' for k, v in self.items()})


class OpenStackClient(object):
    """ Generic OpenStack client. """

    def __init__(self, session=None, verify_ssl=False, **credentials):
        self.verify_ssl = verify_ssl
        if session:
            if isinstance(session, dict):
                logger.debug('Trying to recover OpenStack session.')
                self.session = OpenStackSession.recover(session, verify_ssl=verify_ssl)
                self.session.validate()
            else:
                self.session = session
        else:
            try:
                self.session = OpenStackSession(verify_ssl=verify_ssl, **credentials)
            except AttributeError as e:
                logger.error('Failed to create OpenStack session.')
                six.reraise(OpenStackBackendError, e)

    @property
    def keystone(self):
        return keystone_client.Client(session=self.session.keystone_session, interface='public')

    @property
    def nova(self):
        try:
            return nova_client.Client(version='2', session=self.session.keystone_session, endpoint_type='publicURL')
        except nova_exceptions.ClientException as e:
            logger.exception('Failed to create nova client: %s', e)
            six.reraise(OpenStackBackendError, e)

    @property
    def neutron(self):
        try:
            return neutron_client.Client(session=self.session.keystone_session)
        except neutron_exceptions.NeutronClientException as e:
            logger.exception('Failed to create neutron client: %s', e)
            six.reraise(OpenStackBackendError, e)

    @property
    def cinder(self):
        try:
            return cinder_client.Client(session=self.session.keystone_session)
        except cinder_exceptions.ClientException as e:
            logger.exception('Failed to create cinder client: %s', e)
            six.reraise(OpenStackBackendError, e)

    @property
    def glance(self):
        try:
            return glance_client.Client(session=self.session.keystone_session)
        except glance_exceptions.ClientException as e:
            logger.exception('Failed to create glance client: %s', e)
            six.reraise(OpenStackBackendError, e)

    @property
    def ceilometer(self):
        try:
            return ceilometer_client.Client('2', session=self.session.keystone_session)
        except ceilometer_exceptions.BaseException as e:
            logger.exception('Failed to create ceilometer client: %s', e)
            six.reraise(OpenStackBackendError, e)


class BaseOpenStackBackend(ServiceBackend):

    def __init__(self, settings, tenant_id=None):
        self.settings = settings
        self.tenant_id = tenant_id

    def _get_cached_session_key(self, admin):
        key = 'OPENSTACK_ADMIN_SESSION' if admin else 'OPENSTACK_SESSION_%s' % self.tenant_id
        settings_key = str(self.settings.backend_url) + str(self.settings.password) + str(self.settings.username)
        hashed_settings_key = hashlib.sha256(settings_key).hexdigest()
        return '%s_%s_%s' % (self.settings.uuid.hex, hashed_settings_key, key)

    def get_client(self, name=None, admin=False):
        domain_name = self.settings.domain or 'Default'
        credentials = {
            'auth_url': self.settings.backend_url,
            'username': self.settings.username,
            'password': self.settings.password,
            'user_domain_name': domain_name,
        }
        if self.tenant_id:
            credentials['project_id'] = self.tenant_id
        else:
            credentials['project_domain_name'] = domain_name
            credentials['project_name'] = self.settings.get_option('tenant_name')

        # Skip cache if service settings do no exist
        if not self.settings.uuid:
            return OpenStackClient(**credentials)

        client = None
        attr_name = 'admin_session' if admin else 'session'
        key = self._get_cached_session_key(admin)
        if hasattr(self, attr_name):  # try to get client from object
            client = getattr(self, attr_name)
        elif key in cache:  # try to get session from cache
            session = cache.get(key)
            try:
                client = OpenStackClient(session=session)
            except (OpenStackSessionExpired, OpenStackAuthorizationFailed):
                pass

        if client is None:  # create new token if session is not cached or expired
            client = OpenStackClient(**credentials)
            setattr(self, attr_name, client)  # Cache client in the object
            cache.set(key, dict(client.session), 24 * 60 * 60)  # Add session to cache

        if name:
            return getattr(client, name)
        else:
            return client

    def __getattr__(self, name):
        clients = 'keystone', 'nova', 'neutron', 'cinder', 'glance', 'ceilometer'
        for client in clients:
            if name == '{}_client'.format(client):
                return self.get_client(client, admin=False)

            if name == '{}_admin_client'.format(client):
                return self.get_client(client, admin=True)

        raise AttributeError(
            "'%s' object has no attribute '%s'" % (self.__class__.__name__, name))

    def ping(self, raise_exception=False):
        try:
            self.keystone_client
        except keystone_exceptions.ClientException as e:
            if raise_exception:
                six.reraise(OpenStackBackendError, e)
            return False
        else:
            return True

    def ping_resource(self, instance):
        try:
            self.nova_client.servers.get(instance.backend_id)
        except (ConnectionError, nova_exceptions.ClientException):
            return False
        else:
            return True

    def _pull_tenant_quotas(self, backend_id, scope):
        for quota_name, limit in self.get_tenant_quotas_limits(backend_id).items():
            scope.set_quota_limit(quota_name, limit)
        for quota_name, usage in self.get_tenant_quotas_usage(backend_id).items():
            scope.set_quota_usage(quota_name, usage, fail_silently=True)

    def get_tenant_quotas_limits(self, tenant_backend_id):
        nova = self.nova_client
        neutron = self.neutron_client
        cinder = self.cinder_client

        try:
            nova_quotas = nova.quotas.get(tenant_id=tenant_backend_id)
            cinder_quotas = cinder.quotas.get(tenant_id=tenant_backend_id)
            neutron_quotas = neutron.show_quota(tenant_id=tenant_backend_id)['quota']
        except (nova_exceptions.ClientException,
                cinder_exceptions.ClientException,
                neutron_exceptions.NeutronClientException) as e:
            six.reraise(OpenStackBackendError, e)

        return {
            Tenant.Quotas.ram: nova_quotas.ram,
            Tenant.Quotas.vcpu: nova_quotas.cores,
            Tenant.Quotas.storage: self.gb2mb(cinder_quotas.gigabytes),
            Tenant.Quotas.snapshots: cinder_quotas.snapshots,
            Tenant.Quotas.volumes: cinder_quotas.volumes,
            Tenant.Quotas.instances: nova_quotas.instances,
            Tenant.Quotas.security_group_count: neutron_quotas['security_group'],
            Tenant.Quotas.security_group_rule_count: neutron_quotas['security_group_rule'],
            Tenant.Quotas.floating_ip_count: neutron_quotas['floatingip'],
            Tenant.Quotas.network_count: neutron_quotas['network'],
            Tenant.Quotas.subnet_count: neutron_quotas['subnet'],
        }

    def get_tenant_quotas_usage(self, tenant_backend_id):
        nova = self.nova_client
        neutron = self.neutron_client
        cinder = self.cinder_client
        try:
            volumes = cinder.volumes.list()
            snapshots = cinder.volume_snapshots.list()
            instances = nova.servers.list()
            security_groups = neutron.list_security_groups(tenant_id=tenant_backend_id)['security_groups']
            floating_ips = neutron.list_floatingips(tenant_id=tenant_backend_id)['floatingips']
            networks = neutron.list_networks(tenant_id=tenant_backend_id)['networks']
            subnets = neutron.list_subnets(tenant_id=tenant_backend_id)['subnets']

            flavors = {flavor.id: flavor for flavor in nova.flavors.list()}

            ram, vcpu = 0, 0
            for flavor_id in (instance.flavor['id'] for instance in instances):
                try:
                    flavor = flavors.get(flavor_id, nova.flavors.get(flavor_id))
                except nova_exceptions.NotFound:
                    logger.warning('Cannot find flavor with id %s', flavor_id)
                    continue

                ram += getattr(flavor, 'ram', 0)
                vcpu += getattr(flavor, 'vcpus', 0)

        except (nova_exceptions.ClientException,
                cinder_exceptions.ClientException,
                neutron_exceptions.NeutronClientException) as e:
            six.reraise(OpenStackBackendError, e)

        volumes_size = sum(self.gb2mb(v.size) for v in volumes)
        snapshots_size = sum(self.gb2mb(v.size) for v in snapshots)
        storage = volumes_size + snapshots_size

        return {
            Tenant.Quotas.ram: ram,
            Tenant.Quotas.vcpu: vcpu,
            Tenant.Quotas.storage: storage,
            Tenant.Quotas.volumes: len(volumes),
            Tenant.Quotas.volumes_size: volumes_size,
            Tenant.Quotas.snapshots: len(snapshots),
            Tenant.Quotas.snapshots_size: snapshots_size,
            Tenant.Quotas.instances: len(instances),
            Tenant.Quotas.security_group_count: len(security_groups),
            Tenant.Quotas.security_group_rule_count: len(sum([sg['security_group_rules']
                                                              for sg in security_groups], [])),
            Tenant.Quotas.floating_ip_count: len(floating_ips),
            Tenant.Quotas.network_count: len(networks),
            Tenant.Quotas.subnet_count: len(subnets),
        }

    def _normalize_security_group_rule(self, rule):
        if rule['protocol'] is None:
            rule['protocol'] = ''

        if rule['remote_ip_prefix'] is None:
            rule['remote_ip_prefix'] = '0.0.0.0/0'

        if rule['port_range_min'] is None:
            rule['port_range_min'] = -1

        if rule['port_range_max'] is None:
            rule['port_range_max'] = -1

        return rule

    def _extract_security_group_rules(self, security_group, backend_security_group):
        backend_rules = backend_security_group['security_group_rules']
        cur_rules = {rule.backend_id: rule for rule in security_group.rules.all()}
        for backend_rule in backend_rules:
            # Currently we support only rules for incoming traffic
            if backend_rule['direction'] != 'ingress':
                continue
            cur_rules.pop(backend_rule['id'], None)
            backend_rule = self._normalize_security_group_rule(backend_rule)
            security_group.rules.update_or_create(
                backend_id=backend_rule['id'],
                defaults={
                    'from_port': backend_rule['port_range_min'],
                    'to_port': backend_rule['port_range_max'],
                    'protocol': backend_rule['protocol'],
                    'cidr': backend_rule['remote_ip_prefix'],
                })
        security_group.rules.filter(backend_id__in=cur_rules.keys()).delete()

    def _get_current_properties(self, model):
        return {p.backend_id: p for p in model.objects.filter(settings=self.settings)}

    def _pull_images(self, model_class, filter_function=None):
        glance = self.glance_client
        try:
            images = glance.images.list()
        except glance_exceptions.ClientException as e:
            six.reraise(OpenStackBackendError, e)

        images = [image for image in images if not image['status'] == 'deleted']
        if filter_function:
            images = filter(filter_function, images)

        with transaction.atomic():
            cur_images = self._get_current_properties(model_class)
            for backend_image in images:
                cur_images.pop(backend_image['id'], None)
                model_class.objects.update_or_create(
                    settings=self.settings,
                    backend_id=backend_image['id'],
                    defaults={
                        'name': backend_image['name'],
                        'min_ram': backend_image['min_ram'],
                        'min_disk': self.gb2mb(backend_image['min_disk']),
                    })
            model_class.objects.filter(backend_id__in=cur_images.keys(), settings=self.settings).delete()

    def _delete_backend_floating_ip(self, backend_id, tenant_backend_id):
        neutron = self.neutron_client
        try:
            logger.info("Deleting floating IP %s from tenant %s", backend_id, tenant_backend_id)
            neutron.delete_floatingip(backend_id)
        except neutron_exceptions.NotFound:
            logger.debug("Floating IP %s is already gone from tenant %s", backend_id, tenant_backend_id)
        except neutron_exceptions.NeutronClientException as e:
            six.reraise(OpenStackBackendError, e)
