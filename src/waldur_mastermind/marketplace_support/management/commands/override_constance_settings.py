import os

import yaml
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

    def handle(self, *args, **options):
        with open(options["constance_settings_file"]) as constance_settings_file:
            constance_settings = yaml.safe_load(constance_settings_file)
        if constance_settings is None:
            self.stdout.write(self.style.WARNING("Constance settings file is empty."))
            return

        constance_settings = {
            key.upper(): value for key, value in constance_settings.items()
        }
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
