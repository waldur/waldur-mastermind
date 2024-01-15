import json
import os

import yaml
from constance import config
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand

from waldur_core.core import logos

WHITELABELING_LOGOS = logos.LOGO_MAP.keys()


def make_constance_file_value(image_path):
    image_content = open(image_path, "rb")

    filename = os.path.basename(image_path)
    path = default_storage.save(filename, image_content)
    return os.path.split(path)[1]


class Command(BaseCommand):
    help = """
    Override settings stored in django-constance.The example of .yaml file:
        -   WALDUR_SUPPORT_ENABLED: true # Enables support plugin
            WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE: 'zammad' # Specifies zammad as service desk plugin
            ZAMMAD_API_URL: "https://zammad.example.com/api/" # Specifies zammad API URL
            ZAMMAD_TOKEN: "1282361723491" # Specifies zammad token
            ZAMMAD_GROUP: "default-group" # Specifies zammad group
            ZAMMAD_ARTICLE_TYPE: "email" # Specifies zammad article type
            ZAMMAD_COMMENT_COOLDOWN_DURATION: 7 # Specifies zammad comment cooldown duration
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
            self.stdout.write(self.style.ERROR("Constance settings file is empty."))
            return

        for setting_key, setting_value in constance_settings.items():
            if setting_key.upper() in WHITELABELING_LOGOS:
                if os.path.exists(setting_value):
                    setting_value = make_constance_file_value(setting_value)
                else:
                    self.stdout.write(
                        self.style.ERROR(f"{setting_key.upper()} file does not exist.")
                    )
                    continue
            if isinstance(setting_value, dict):
                setting_value = json.dumps(setting_value)

            setattr(config, setting_key.upper(), setting_value)
            self.stdout.write(
                self.style.SUCCESS(
                    f"{setting_key.upper()} has been set to {setting_value}."
                )
            )
