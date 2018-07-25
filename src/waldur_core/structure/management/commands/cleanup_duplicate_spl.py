from django.core.management.base import BaseCommand

from waldur_core.structure.models import ServiceProjectLink


class Command(BaseCommand):
    help = """ Cleanup duplicate service project links """

    def handle(self, *args, **options):
        deleted_count = 0
        for model in ServiceProjectLink.get_all_models():
            seen = set()
            for obj in model.objects.all():
                key = (obj.service.pk, obj.project.pk)
                if key in seen:
                    obj.delete()
                    deleted_count += 1
                else:
                    seen.add(key)

        if deleted_count == 0:
            self.stdout.write('Duplicate service project links not found')
        else:
            self.stdout.write('%s duplicate service project links deleted' % deleted_count)
