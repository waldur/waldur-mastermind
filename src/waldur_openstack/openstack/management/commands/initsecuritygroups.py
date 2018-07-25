from __future__ import unicode_literals

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ... import models, executors, handlers


class Command(BaseCommand):
    help_text = "Add default security groups with given names to all tenants."

    def add_arguments(self, parser):
        parser.add_argument('names', nargs='+', type=str)

    def handle(self, *args, **options):
        names = options['names']
        default_security_groups = getattr(settings, 'WALDUR_OPENSTACK', {}).get('DEFAULT_SECURITY_GROUPS')
        security_groups = []
        for name in names:
            try:
                group = next(sg for sg in default_security_groups if sg['name'] == name)
            except StopIteration:
                raise CommandError('There is no default security group with name %s' % name)
            else:
                security_groups.append(group)

        for tenant in models.Tenant.objects.all():
            for group in security_groups:
                if tenant.security_groups.filter(name=group['name']).exists():
                    self.stdout.write('Tenant %s already has security group %s' % (tenant, group['name']))
                    continue
                tenant.security_groups.create(name=group['name'],
                                              description=group['description'],
                                              service_project_link=tenant.service_project_link,
                                              )
                try:
                    db_security_group = handlers.create_security_group(tenant, group)
                except handlers.SecurityGroupCreateException as e:
                    self.stdout.write(
                        'Failed to add security_group %s to tenant %s. Error: %s' % (group['name'], tenant, e))
                else:
                    try:
                        executors.SecurityGroupCreateExecutor.execute(db_security_group, async=False)
                    except Exception as e:
                        self.stdout.write('Failed to add security group %s to tenant %s. Error: %s' %
                                          (db_security_group, tenant, e))
                    else:
                        self.stdout.write(
                            'Security group %s has been successfully added to tenant %s' % (group['name'], tenant))
