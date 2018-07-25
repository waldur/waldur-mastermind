from __future__ import unicode_literals

import collections
import cStringIO
import csv

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
import python_freeipa

from waldur_core.structure import models as structure_models
from waldur_core.quotas import models as quota_models

from . import models, utils


class GroupSynchronizer(object):
    """
    This class maps Waldur structure units to FreeIPA groups and memberships.

    1) Customers and projects are modelled as FreeIPA groups.
    2) Group name for customer looks like waldur_org_590a409656054fc284b1102829406c63
       Group name for project looks like waldur_project_590a409656054fc284b1102829406c63
    2) Customer name is stored as group description.
       Similarly, project name is stored as group description.
    3) Project group is modelled as member of customer group.
    4) Permissions are mapped to group memberships.

    Synchronization is performed in batch processing task and consists of the following steps:

    1) Convert Waldur entities to group mappings.
    2) Convert FreeIPA entities to group mappings.
    3) Compare group mappings and apply series of API calls to synchronize them.

    Note that in order to distinguish between Waldur managed groups and internal FreeIPA groups,
    group name prefix should be specified. Similarly, there's also user name prefix setting available.
    """

    def __init__(self, client):
        self.client = client
        self.prefix = settings.WALDUR_FREEIPA['GROUPNAME_PREFIX']

        self.profiles = {
            profile.user_id: profile.username
            for profile in models.Profile.objects.all()
        }

        self.groups = set()
        self.group_users = collections.defaultdict(set)
        self.group_children = collections.defaultdict(set)
        self.group_names = dict()

        self.freeipa_groups = set()
        self.freeipa_users = collections.defaultdict(set)
        self.freeipa_children = collections.defaultdict(set)
        self.freeipa_names = dict()

    def group_name(self, key):
        return '%s%s' % (self.prefix, key)

    def project_group_name(self, project):
        return self.group_name('project_%s' % project.uuid)

    def customer_group_name(self, customer):
        return self.group_name('org_%s' % customer.uuid)

    def get_group_description(self, name, limit):
        stream = cStringIO.StringIO()
        writer = csv.writer(stream)
        writer.writerow([name.encode('utf-8'), str(limit)])
        return stream.getvalue().strip().decode('utf-8')

    def add_customer(self, customer, limit):
        group = self.customer_group_name(customer)
        self.groups.add(group)
        self.group_names[group] = self.get_group_description(customer.name, limit)

    def add_project(self, project, limit):
        project_group = self.project_group_name(project)
        self.groups.add(project_group)
        self.group_names[project_group] = self.get_group_description(project.name, limit)

        customer_group = self.customer_group_name(project.customer)
        self.groups.add(customer_group)

        self.group_children[customer_group].add(project_group)

    def add_customer_user(self, customer, user):
        username = self.profiles.get(user.id)
        if username:
            group = self.customer_group_name(customer)
            self.group_users[group].add(username)

    def add_project_user(self, project, user):
        username = self.profiles.get(user.id)
        if username:
            group = self.project_group_name(project)
            self.group_users[group].add(username)

    def collect_waldur_permissions(self):
        for permission in structure_models.CustomerPermission.objects.filter(is_active=True):
            self.add_customer_user(permission.customer, permission.user)

        for permission in structure_models.ProjectPermission.objects.filter(is_active=True):
            self.add_project_user(permission.project, permission.user)

    def get_limits(self, model):
        ctype = ContentType.objects.get_for_model(model)
        customer_quotas = quota_models.Quota.objects.filter(
            content_type=ctype, name=utils.QUOTA_NAME
        ).only('object_id', 'limit')
        return {
            quota.object_id: quota.limit
            for quota in customer_quotas
        }

    def collect_waldur_customers(self):
        limits = self.get_limits(structure_models.Customer)
        for customer in structure_models.Customer.objects.all():
            limit = limits.get(customer.id, -1.0)
            self.add_customer(customer, limit)

    def collect_waldur_projects(self):
        limits = self.get_limits(structure_models.Project)
        for project in structure_models.Project.objects.all():
            limit = limits.get(project.id, -1.0)
            self.add_project(project, limit)

    def add_freeipa_group(self, groupname, description, children):
        self.freeipa_groups.add(groupname)
        if description:
            self.freeipa_names[groupname] = description[0]
        self.freeipa_children[groupname] = set(child for child in children if child.startswith(self.prefix))

    def add_freeipa_users(self, groupname, users):
        self.freeipa_users[groupname].update(users)

    def collect_freeipa_groups(self):
        backend_groups = self.client.group_find()['result']
        for group in backend_groups:
            groupname = group['cn'][0]

            # Ignore groups not marked by own prefix
            if not groupname.startswith(self.prefix):
                continue

            members = group.get('member_user', [])
            description = group.get('description')
            children = group.get('member_group', [])
            self.add_freeipa_group(groupname, description, children)
            self.add_freeipa_users(groupname, members)

    def add_missing_groups(self):
        missing_groups = self.groups - self.freeipa_groups
        for group in missing_groups:
            utils.renew_task_status()
            self.client.group_add(group, description=self.group_names.get(group))

    def sync_group_names(self):
        for group in self.groups & self.freeipa_groups:
            utils.renew_task_status()
            waldur_name = self.group_names.get(group)
            freeipa_name = self.freeipa_names.get(group)
            if waldur_name != freeipa_name:
                self.client.group_mod(group, description=waldur_name)

    def sync_members(self):
        for group in self.groups:
            utils.renew_task_status()
            waldur_members = self.group_users.get(group, set())
            backend_members = self.freeipa_users.get(group, set())

            new_members = list(waldur_members - backend_members)
            if new_members:
                self.client.group_add_member(group, users=new_members, skip_errors=True)

            stale_members = list(backend_members - waldur_members)
            if stale_members:
                self.client.group_remove_member(group, users=stale_members, skip_errors=True)

    def sync_children(self):
        for group in self.groups:
            utils.renew_task_status()
            waldur_children = self.group_children.get(group, set())
            freeipa_children = self.freeipa_children.get(group, set())

            missing_children = list(waldur_children - freeipa_children)
            if missing_children:
                self.client.group_add_member(group, groups=missing_children, skip_errors=True)

            stale_children = list(freeipa_children - waldur_children)
            if stale_children:
                self.client.group_remove_member(group, groups=stale_children, skip_errors=True)

    def delete_stale_groups(self):
        for group in self.freeipa_groups - self.groups:
            utils.renew_task_status()
            self.client.group_del(group)

    def sync(self):
        try:
            self.collect_waldur_permissions()
            self.collect_waldur_customers()
            self.collect_waldur_projects()
            self.collect_freeipa_groups()

            self.add_missing_groups()
            self.sync_group_names()
            self.sync_members()
            self.sync_children()
            self.delete_stale_groups()

        finally:
            utils.release_task_status()


class FreeIPABackend(object):
    def __init__(self):
        options = settings.WALDUR_FREEIPA
        self._client = python_freeipa.Client(
            host=options['HOSTNAME'],
            verify_ssl=options['VERIFY_SSL']
        )
        self._client.login(options['USERNAME'], options['PASSWORD'])

    def _format_ssh_keys(self, user):
        return list(user.sshpublickey_set.values_list('public_key', flat=True))

    def create_profile(self, profile):
        waldur_user = profile.user
        ssh_keys = self._format_ssh_keys(waldur_user)
        first_name, last_name, _ = utils.get_names(profile.user.full_name)

        self._client.user_add(
            username=profile.username,
            first_name=first_name,
            last_name=last_name,
            full_name=waldur_user.full_name,
            mail=waldur_user.email,
            job_title=waldur_user.job_title,
            preferred_language=waldur_user.preferred_language,
            telephonenumber=waldur_user.phone_number,
            ssh_key=ssh_keys,
            gecos=profile.gecos,
        )

    def disable_profile(self, profile):
        self._client.user_disable(profile.username)

    def enable_profile(self, profile):
        self._client.user_enable(profile.username)

    def update_ssh_keys(self, profile):
        ssh_keys = self._format_ssh_keys(profile.user)
        if ssh_keys:
            ssh_keys = sorted(ssh_keys)
        else:
            ssh_keys = None

        backend_profile = self._client.user_show(profile.username)
        backend_keys = backend_profile.get('ipasshpubkey')
        if backend_keys:
            backend_keys = sorted(backend_keys)

        if backend_keys != ssh_keys:
            self._client.user_mod(profile.username, ipasshpubkey=ssh_keys)

    def _update_profile(self, profile, params):
        try:
            self._client.user_mod(profile.username, **params)
        except python_freeipa.exceptions.BadRequest as e:
            # If no modifications to be performed freeipa-server return an exception.
            if e.code == 4202:
                pass

    def update_name(self, profile):
        first_name, last_name, initials = utils.get_names(profile.user.full_name)
        params = {
            'givenname': first_name,
            'sn': last_name,
            'cn': profile.user.full_name,
            'displayname': profile.user.full_name,
            'initials': initials,
        }
        self._update_profile(profile, params)

    def update_gecos(self, profile):
        params = {
            'gecos': profile.gecos,
        }
        self._update_profile(profile, params)

    def synchronize_names(self):
        for profile in models.Profile.objects.filter(is_active=True):
            self.update_name(profile)

    def synchronize_gecos(self):
        for profile in models.Profile.objects.filter(is_active=True):
            self.update_gecos(profile)

    def synchronize_groups(self):
        synchronizer = GroupSynchronizer(self._client)
        synchronizer.sync()
