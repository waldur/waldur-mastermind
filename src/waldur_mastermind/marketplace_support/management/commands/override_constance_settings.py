import os

import yaml
from constance.models import Constance
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand
from rest_framework.exceptions import ValidationError

from waldur_core.core import logos
from waldur_core.core.serializers import ConstanceSettingsSerializer

WHITELABELING_LOGOS = logos.LOGO_MAP.keys()


def make_constance_file_value(image_path):
    image_content = open(image_path, "rb")

    filename = os.path.basename(image_path)
    path = default_storage.save(filename, image_content)
    return os.path.split(path)[1]


class Command(BaseCommand):
    help = """
    Override settings stored in django-constance. The example of .yaml file:

    ```yaml
      - WALDUR_SUPPORT_ENABLED: true # Enables support plugin
        WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE: 'zammad' # Specifies zammad as service desk plugin
        ZAMMAD_API_URL: "https://zammad.example.com/api/" # Specifies zammad API URL
        ZAMMAD_TOKEN: "1282361723491" # Specifies zammad token
        ZAMMAD_GROUP: "default-group" # Specifies zammad group
        ZAMMAD_ARTICLE_TYPE: "email" # Specifies zammad article type
        ZAMMAD_COMMENT_COOLDOWN_DURATION: 7 # Specifies zammad comment cooldown duration
    ```
    """

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "constance_settings_file",
            help="Specifies location of file in YAML format containing new settings",
        )
        parser.add_argument(
            "--if-unset",
            action="store_true",
            help=(
                "Seed rather than override: skip any setting that already has a "
                "stored value, so a change made in the UI survives the next run. "
                "Use this for settings an administrator is expected to manage."
            ),
        )

    def drop_already_set(self, constance_settings):
        """Keep only the settings that have never been stored.

        django-constance writes a row the first time a setting is given a value,
        whether by this command or by an administrator in the UI, and falls back
        to the code default while no row exists. So the presence of a row is
        what separates "nobody has ever chosen" from "somebody chose this" —
        and the latter is not ours to overwrite on every container start.
        """
        already_set = set(
            Constance.objects.filter(key__in=constance_settings).values_list(
                "key", flat=True
            )
        )
        for key in sorted(already_set):
            self.stdout.write(f"{key} already has a stored value, leaving it as it is.")
        return {
            key: value
            for key, value in constance_settings.items()
            if key not in already_set
        }

    def handle(self, *args, **options):
        with open(options["constance_settings_file"]) as constance_settings_file:
            constance_settings = yaml.safe_load(constance_settings_file)
        if constance_settings is None:
            self.stdout.write(self.style.WARNING("Constance settings file is empty."))
            return

        constance_settings = {
            key.upper(): value for key, value in constance_settings.items()
        }

        if options["if_unset"]:
            constance_settings = self.drop_already_set(constance_settings)
            if not constance_settings:
                return

        keys_to_delete = []
        for setting_key, setting_value in constance_settings.items():
            if setting_key in WHITELABELING_LOGOS:
                if not setting_value.strip():
                    setting_value = None
                elif os.path.exists(setting_value):
                    with open(setting_value, "rb") as image_file:
                        image_content = image_file.read()
                        setting_value = SimpleUploadedFile(setting_value, image_content)
                else:
                    self.stdout.write(
                        self.style.ERROR(f"{setting_key.upper()} file does not exist.")
                    )
                    keys_to_delete.append(setting_key)
                    continue
                constance_settings[setting_key] = setting_value

        # Delete keys that have invalid values
        for key in keys_to_delete:
            del constance_settings[key]

        try:
            serializer = ConstanceSettingsSerializer(data=constance_settings)
            serializer.is_valid(raise_exception=False)

            # settings_to_delete = [key for key,_ in serializer.errors.items()]
            settings_to_delete = dict(serializer.errors)

            # Remove broken fields from the data
            for name, _ in settings_to_delete.items():
                del constance_settings[name]

            serializer = ConstanceSettingsSerializer(data=constance_settings)
            if serializer.is_valid():
                serializer.save()

            # Show error messages for broken fields
            for name, error in settings_to_delete.items():
                self.stdout.write(
                    self.style.ERROR(
                        f"Failed to save setting {name} due to error: {str(error[0])}"
                    )
                )
        except ValidationError as e:
            self.stdout.write(
                self.style.ERROR(f"Unexpected validation error: {str(e)}")
            )

        for setting_key, setting_value in constance_settings.items():
            if "password" in setting_key.lower() or "token" in setting_key.lower():
                setting_value = "<redacted>"

            self.stdout.write(
                self.style.SUCCESS(
                    f"{setting_key.upper()} has been set to {setting_value}."
                )
            )
