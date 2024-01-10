from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ... import executors, handlers, models


class Command(BaseCommand):
    help = "Add default security groups with given names to all tenants."

    def add_arguments(self, parser):
        parser.add_argument("names", nargs="+", type=str)

    def handle(self, *args, **options):
        names = options["names"]
        default_security_groups = getattr(settings, "WALDUR_OPENSTACK", {}).get(
            "DEFAULT_SECURITY_GROUPS"
        )
        security_groups = []
        for name in names:
            try:
                group = next(sg for sg in default_security_groups if sg["name"] == name)
            except StopIteration:
                raise CommandError(
                    "There is no default security group with name %s" % name
                )
            else:
                security_groups.append(group)

        for tenant in models.Tenant.objects.all():
            for group in security_groups:
                if tenant.security_groups.filter(name=group["name"]).exists():
                    self.stdout.write(
                        "Tenant {} already has security group {}".format(
                            tenant, group["name"]
                        )
                    )
                    continue
                tenant.security_groups.create(
                    name=group["name"],
                    description=group["description"],
                    service_settings=tenant.service_settings,
                    project=tenant.project,
                )
                try:
                    db_security_group = handlers.create_security_group(tenant, group)
                except handlers.SecurityGroupCreateException as e:
                    self.stdout.write(
                        "Failed to add security_group {} to tenant {}. Error: {}".format(
                            group["name"], tenant, e
                        )
                    )
                else:
                    try:
                        executors.SecurityGroupCreateExecutor.execute(
                            db_security_group, is_async=False
                        )
                    except Exception as e:
                        self.stdout.write(
                            f"Failed to add security group {db_security_group} to tenant {tenant}. Error: {e}"
                        )
                    else:
                        self.stdout.write(
                            "Security group {} has been successfully added to tenant {}".format(
                                group["name"], tenant
                            )
                        )
