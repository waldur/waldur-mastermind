from django.core.management.base import BaseCommand

from waldur_core.core.models import User
from waldur_mastermind.marketplace.models import Order, OrderItem, Resource


class Command(BaseCommand):
    help = """Create marketplace order for each resource if it does not yet exist."""

    def handle(self, *args, **options):
        default_user = User.objects.filter(is_staff=True).first()
        existing_resources = OrderItem.objects.exclude(resource_id=None)\
            .values_list('resource_id', flat=True).distinct()
        missing_resources = Resource.objects.exclude(id__in=existing_resources)
        for resource in missing_resources:
            order = Order.objects.create(
                created=resource.created,
                modified=resource.modified,
                created_by=default_user,
                approved_by=default_user,
                approved_at=resource.created,
                project=resource.project,
                state=Order.States.DONE,
            )
            OrderItem.objects.create(
                order=order,
                resource=resource,
                offering=resource.offering,
                attributes=resource.attributes,
                limits=resource.limits,
                plan=resource.plan,
                state=OrderItem.States.DONE,
            )
        count = missing_resources.count()
        if count == 0:
            self.stdout.write(self.style.SUCCESS('There are no resources without orders.'))
        if count == 1:
            self.stdout.write(self.style.SUCCESS('%s order has been created.' % count))
        else:
            self.stdout.write(self.style.SUCCESS('%s orders have been created.' % count))
