import logging
import operator
import re
from functools import reduce

from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone

from waldur_core.structure import ServiceBackend, ServiceBackendError
from waldur_freeipa import models as freeipa_models
from waldur_slurm.client import SlurmClient
from waldur_slurm.structures import Quotas

from . import base, models

logger = logging.getLogger(__name__)


class SlurmBackend(ServiceBackend):
    def __init__(self, settings):
        self.settings = settings
        self.client = self.get_client(settings)

    def get_client(self, settings):
        return SlurmClient(
            hostname=settings.options.get('hostname', 'localhost'),
            username=settings.username or 'root',
            port=settings.options.get('port', 22),
            key_path=django_settings.WALDUR_SLURM['PRIVATE_KEY_PATH'],
            use_sudo=settings.options.get('use_sudo', False),
        )

    def pull_resources(self):
        for allocation in self.get_allocation_queryset().filter(
            state=models.Allocation.States.OK
        ):
            try:
                self.pull_allocation(allocation)
            except Exception as e:
                logger.error('Error while pulling allocation [%s]: %s', allocation, e)
        self.sync_usage()

    def ping(self, raise_exception=False):
        try:
            self.client.list_accounts()
        except base.BatchError as e:
            if raise_exception:
                raise ServiceBackendError(e)
            return False
        else:
            return True

    def add_new_users(self, allocation):
        users = allocation.service_project_link.project.customer.get_users()
        for profile in freeipa_models.Profile.objects.all(user__in=users):
            self.add_user(allocation, profile.username.lower())

    def create_allocation(self, allocation):
        project = allocation.service_project_link.project
        customer_account = self.get_customer_name(project.customer)
        project_account = self.get_project_name(project)
        allocation_account = self.get_allocation_name(allocation)

        if not self.client.get_account(customer_account):
            self.create_customer(project.customer)

        if not self.client.get_account(project_account):
            self.create_project(project)

        self.client.create_account(
            name=allocation_account,
            description=allocation.name,
            organization=project_account,
        )
        allocation.backend_id = allocation_account
        allocation.save()

        self.set_resource_limits(allocation)
        self.add_new_users(allocation)

    def delete_allocation(self, allocation):
        account = allocation.backend_id

        if not account.strip():
            raise ServiceBackendError(
                'Empty backend_id for allocation: %s' % allocation
            )

        if self.client.get_account(account):
            self.client.delete_account(account)

        project = allocation.service_project_link.project
        if self.get_allocation_queryset().filter(project=project).count() == 0:
            self.delete_project(project)

        if (
            self.get_allocation_queryset()
            .filter(project__customer=project.customer)
            .count()
            == 0
        ):
            self.delete_customer(project.customer)

    def add_user(self, allocation, username):
        """
        Create association between user and SLURM account if it does not exist yet.
        """
        account = allocation.backend_id

        if not account.strip():
            raise ServiceBackendError(
                'Empty backend_id for allocation: %s' % allocation
            )

        default_account = self.settings.options.get('default_account')
        if not self.client.get_association(username, account):
            self.client.create_association(username, account, default_account)

    def delete_user(self, allocation, username):
        """
        Delete association between user and SLURM account if it exists.
        """
        account = allocation.backend_id

        if not account.strip():
            raise ServiceBackendError(
                'Empty backend_id for allocation: %s' % allocation
            )

        if self.client.get_association(username, account):
            self.client.delete_association(username, account)

    def set_resource_limits(self, allocation):
        # TODO: add default limits configuration (https://opennode.atlassian.net/browse/WAL-3037)
        default_limits = django_settings.WALDUR_SLURM['DEFAULT_LIMITS']
        quotas = Quotas(
            cpu=default_limits['CPU'],
            gpu=default_limits['GPU'],
            ram=default_limits['RAM'],
        )

        self.client.set_resource_limits(allocation.backend_id, quotas)

    def sync_usage(self):
        waldur_allocations = {
            allocation.backend_id: allocation
            for allocation in self.get_allocation_queryset()
            if allocation.backend_id
        }

        report = self.get_usage_report(waldur_allocations.keys())
        for account, usage in report.items():
            allocation = waldur_allocations.get(account)
            if not allocation:
                logger.debug(
                    'Skipping usage report for account %s because it is not managed under Waldur',
                    account,
                )
                continue
            self._update_quotas(allocation, usage)

    def pull_allocation(self, allocation):
        account = allocation.backend_id

        if not account.strip():
            raise ServiceBackendError(
                'Empty backend_id for allocation: %s' % allocation
            )

        report = self.get_usage_report([account])
        usage = report.get(account)
        if not usage:
            usage = {'TOTAL_ACCOUNT_USAGE': Quotas()}
        self._update_quotas(allocation, usage)
        limits = self.get_allocation_limits(account)
        self._update_limits(allocation, limits)

        self._update_allocation_associations(allocation)

    def get_usage_report(self, accounts):
        report = {}
        lines = self.client.get_usage_report(accounts)

        for line in lines:
            report.setdefault(line.account, {}).setdefault(line.user, Quotas())
            report[line.account][line.user] += line.quotas

        for usage in report.values():
            quotas = usage.values()
            total = reduce(operator.add, quotas)
            usage['TOTAL_ACCOUNT_USAGE'] = total

        return report

    def get_allocation_limits(self, account):
        lines = self.client.get_resource_limits(account)
        correct_lines = [
            association for association in lines if association.resource_limits
        ]
        if len(correct_lines) > 0:
            line = correct_lines[0]
            limits = Quotas(cpu=line.cpu, gpu=line.gpu, ram=line.ram)
            return limits

    def _update_limits(self, allocation, limits):
        if not limits:
            return
        allocation.cpu_limit = limits.cpu
        allocation.gpu_limit = limits.gpu
        allocation.ram_limit = limits.ram
        allocation.save(update_fields=['cpu_limit', 'gpu_limit', 'ram_limit'])

    @transaction.atomic()
    def _update_quotas(self, allocation, usage):
        quotas = usage.pop('TOTAL_ACCOUNT_USAGE')
        allocation.cpu_usage = quotas.cpu
        allocation.gpu_usage = quotas.gpu
        allocation.ram_usage = quotas.ram
        allocation.save(update_fields=['cpu_usage', 'gpu_usage', 'ram_usage'])

        usernames = usage.keys()
        usermap = {
            profile.username: profile.user
            for profile in freeipa_models.Profile.objects.filter(username__in=usernames)
        }

        allocation_usage, _ = models.AllocationUsage.objects.update_or_create(
            allocation=allocation,
            year=timezone.now().year,
            month=timezone.now().month,
            defaults={
                'cpu_usage': quotas.cpu,
                'gpu_usage': quotas.gpu,
                'ram_usage': quotas.ram,
            },
        )

        for username, quotas in usage.items():
            models.AllocationUserUsage.objects.update_or_create(
                allocation_usage=allocation_usage,
                user=usermap.get(username),
                username=username,
                defaults={
                    'cpu_usage': quotas.cpu,
                    'gpu_usage': quotas.gpu,
                    'ram_usage': quotas.ram,
                },
            )

    def create_customer(self, customer):
        customer_name = self.get_customer_name(customer)
        return self.client.create_account(customer_name, customer.name, customer_name)

    def delete_customer(self, customer_uuid):
        self.client.delete_account(self.get_customer_name(customer_uuid))

    def create_project(self, project):
        name = self.get_project_name(project)
        parent_name = self.get_customer_name(project.customer)
        return self.client.create_account(name, project.name, name, parent_name)

    def delete_project(self, project_uuid):
        self.client.delete_account(self.get_project_name(project_uuid))

    def get_allocation_queryset(self):
        return models.Allocation.objects.filter(
            service_project_link__service__settings=self.settings
        )

    def get_customer_name(self, customer):
        return self.get_account_name(
            django_settings.WALDUR_SLURM['CUSTOMER_PREFIX'], customer
        )

    def get_project_name(self, project):
        return self.get_account_name(
            django_settings.WALDUR_SLURM['PROJECT_PREFIX'], project
        )

    def sanitize_allocation_name(self, name):
        incorrect_symbols_regex = r'[^%s]+' % models.SLURM_ALLOCATION_REGEX
        return re.sub(incorrect_symbols_regex, '', name)

    def get_allocation_name(self, allocation):
        prefix = django_settings.WALDUR_SLURM['ALLOCATION_PREFIX']
        name = allocation.name
        hexpart = allocation.uuid.hex[:5]
        raw_name = "%s%s_%s" % (prefix, hexpart, name)
        result_name = self.sanitize_allocation_name(raw_name)[
            : models.SLURM_ALLOCATION_NAME_MAX_LEN
        ]
        return result_name.lower()

    def get_account_name(self, prefix, object_or_uuid):
        key = (
            isinstance(object_or_uuid, str)
            and object_or_uuid
            or object_or_uuid.uuid.hex
        )
        return '%s%s' % (prefix, key)

    def _update_allocation_associations(self, allocation):
        backend_usernames = self.client.list_account_users(allocation.backend_id)

        local_usernames = [
            association.username for association in allocation.associations.all()
        ]
        stale_usernames = set(local_usernames) - set(backend_usernames)
        models.Association.objects.filter(
            allocation=allocation, username__in=stale_usernames
        ).delete()
        new_usernames = set(backend_usernames) - set(local_usernames)

        for username in new_usernames:
            models.Association.objects.create(
                allocation=allocation, username=username,
            )
