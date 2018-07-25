from django.core.management.base import BaseCommand
from reversion.models import Version
from six.moves import input

from waldur_core.quotas.models import Quota


class Command(BaseCommand):
    help = "Delete quotas versions duplicates."

    def handle(self, *args, **options):
        self.stdout.write('Collecting duplicates...')
        duplicates = sum([self.get_quota_duplicate_versions(quota) for quota in Quota.objects.all()], [])
        self.stdout.write('...Done')

        if not duplicates:
            self.stdout.write('No duplicates were found. Congratulations!')
        else:
            self.stdout.write('There are %s duplicates for quotas versions.' % len(duplicates))
            while True:
                delete = input('  Do you want to delete them? [Y/n]:') or 'y'
                if delete.lower() not in ('y', 'n'):
                    self.stdout.write('  Please enter letter "y" or "n"')
                else:
                    delete = delete.lower() == 'y'
                    break
            if delete:
                for duplicate in duplicates:
                    duplicate.delete()
                self.stdout.write('All duplicates were deleted.')
            else:
                self.stdout.write('Duplicates were not deleted.')

    def get_quota_duplicate_versions(self, quota):
        versions = Version.objects.get_for_object(quota).order_by('revision__date_created')
        if not versions:
            return []
        duplicates = []
        last_version = versions[0]
        for version in versions[1:]:
            next_version = version
            if self.are_versions_equal(quota, last_version, next_version):
                duplicates.append(next_version)
            else:
                last_version = next_version
        return duplicates

    def are_versions_equal(self, quota, v1, v2):
        o1 = v1._object_version.object
        o2 = v2._object_version.object
        return all([getattr(o1, f) == getattr(o2, f) for f in quota.get_version_fields()])
