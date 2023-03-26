import json

from waldur_core.core.features import FEATURES
from waldur_core.core.models import Feature
from waldur_core.core.utils import DryRunCommand


class Command(DryRunCommand):
    help = "Import features in JSON format"

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            'features_file',
            help='Specifies location of features file.',
        )

    def handle(self, *args, **options):
        valid_features = {
            f'{section["key"]}.{feature["key"]}'
            for section in FEATURES
            for feature in section['items']
        }
        with open(options['features_file']) as features_file:
            features = json.load(features_file)

            invalid_features = set(features.keys()) - valid_features
            if invalid_features:
                self.stdout.write(
                    self.style.WARNING(
                        f'Invalid features detected: {", ".join(invalid_features)}'
                    )
                )

            if options['dry_run']:
                for key, new_value in features.items():
                    try:
                        old_value = Feature.objects.get(key=key).value
                    except Feature.DoesNotExist:
                        old_value = False
                    if old_value != new_value:
                        self.stdout.write(
                            self.style.SUCCESS(
                                f'Feature {key} would be changed from {old_value} to {new_value}'
                            )
                        )
            else:
                changed = 0
                for key, value in features.items():
                    try:
                        feature = Feature.objects.get(key=key)
                        if feature.value != value:
                            feature.value = value
                            feature.save(update_fields=['value'])
                            self.style.NOTICE(f'Setting {key} to {value}.')
                            changed += 1
                    except Feature.DoesNotExist:
                        Feature.objects.create(key=key, value=value)
                        changed += 1
                if changed == 0:
                    self.stdout.write(
                        self.style.SUCCESS('No features have been updated.')
                    )
                elif changed == 1:
                    self.stdout.write(self.style.SUCCESS('1 feature has been updated.'))
                else:
                    self.stdout.write(
                        self.style.SUCCESS(f'{changed} features have been updated.')
                    )
