import os

import yaml
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from rest_framework.exceptions import ValidationError

from waldur_core.configuration import registry
from waldur_core.configuration.models import Setting, Source
from waldur_core.configuration.secrets import is_constance_secret
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
    Override runtime settings from a YAML file. The example of .yaml file:

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
            # The keyword callers of override_constance_settings pass.
            "constance_settings_file",
            metavar="settings_file",
            nargs="?",
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
        parser.add_argument(
            "--lock",
            action="store_true",
            help=(
                "For a file the deployment applies on every deploy: record the "
                "file as managing its settings and lock them, so that they "
                "cannot be changed in the UI only to be reverted on the next "
                "deploy. Settings the file managed before but no longer holds "
                "are handed back to administrators."
            ),
        )
        parser.add_argument(
            "--unlock",
            action="append",
            default=[],
            metavar="SETTING",
            help=(
                "Let administrators change SETTING in the UI again, keeping its "
                "value. Can be repeated, and needs no file. This lasts until "
                "the next deploy: a --lock file that still holds the setting "
                "puts its value back and locks it again."
            ),
        )

    def drop_already_set(self, constance_settings):
        """Keep only the settings that have never been stored.

        A setting has a row once it is given a value, whether by this command
        or by an administrator in the UI, and reads as the code default while
        no row exists. So the presence of a row is what separates "nobody has
        ever chosen" from "somebody chose this" — and the latter is not ours to
        overwrite on every container start.
        """
        already_set = set(
            Setting.objects.filter(key__in=constance_settings).values_list(
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

    def unlock(self, keys):
        keys = [key.upper() for key in keys]
        unknown = [key for key in keys if not registry.has(key)]
        if unknown:
            raise CommandError("Unknown settings: %s" % ", ".join(unknown))
        locks = Setting.objects.locks(*keys)
        Setting.objects.unlock(*keys)
        for key in keys:
            if key not in locks:
                self.stdout.write(f"{key} is not locked.")
                continue
            self.stdout.write(self.style.SUCCESS(f"{key} has been unlocked."))
            self.stdout.write(
                self.style.WARNING(
                    f"The next deploy puts back the value of {key} from "
                    f"{locks[key]} and locks it again, unless it is taken out "
                    "of that file."
                )
            )

    def release_dropped(self, file_name, kept=()):
        """Hand back the settings this file managed before but no longer does:
        the ones it no longer holds, or all of them when it is now applied as
        a seed."""
        dropped = sorted(
            Setting.objects.filter(managed_by=file_name)
            .exclude(key__in=kept)
            .values_list("key", flat=True)
        )
        if dropped:
            Setting.objects.release(*dropped)
            self.stdout.write(
                f"{', '.join(dropped)} no longer managed by {file_name}, "
                "handed back to administrators."
            )

    def drop_unknown(self, constance_settings):
        unknown = sorted(key for key in constance_settings if not registry.has(key))
        for key in unknown:
            self.stdout.write(
                self.style.ERROR(f"{key} is not a setting Waldur knows, skipped.")
            )
        return {
            key: value
            for key, value in constance_settings.items()
            if key not in unknown
        }

    def drop_runtime_already_set(self, constance_settings):
        runtime = {
            key: value
            for key, value in constance_settings.items()
            if key in registry.RUNTIME_SETTINGS
        }
        kept = self.drop_already_set(runtime)
        return {
            key: value
            for key, value in constance_settings.items()
            if key not in runtime or key in kept
        }

    def handle(self, *args, **options):
        if options["lock"] and options["if_unset"]:
            raise CommandError("--lock and --if-unset exclude each other.")
        if options["unlock"]:
            self.unlock(options["unlock"])
        if not options["constance_settings_file"]:
            if not options["unlock"]:
                raise CommandError("Specify a settings file or --unlock.")
            return

        file_name = os.path.basename(options["constance_settings_file"])
        with open(options["constance_settings_file"]) as constance_settings_file:
            constance_settings = yaml.safe_load(constance_settings_file)
        # A one-off run manages nothing, so it hands nothing back either.
        manages = options["lock"] or options["if_unset"]
        if constance_settings is None:
            self.stdout.write(self.style.WARNING("Settings file is empty."))
            if manages:
                self.release_dropped(file_name)
            return

        constance_settings = self.drop_unknown(
            {key.upper(): value for key, value in constance_settings.items()}
        )

        if options["if_unset"]:
            # A file switched from override to seed hands its settings over.
            self.release_dropped(file_name)
            constance_settings = self.drop_already_set(constance_settings)
            if not constance_settings:
                return
        else:
            constance_settings = self.drop_runtime_already_set(constance_settings)
            if options["lock"]:
                # Before invalid values are dropped below: a setting the file
                # still holds stays the deployment's even while its value is
                # rejected.
                self.release_dropped(
                    file_name,
                    [
                        key
                        for key in constance_settings
                        if key not in registry.RUNTIME_SETTINGS
                    ],
                )

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
                serializer.save(
                    source=Source.DEPLOYMENT,
                    source_detail=file_name,
                    restamp_unchanged=True,
                    lock=options["lock"],
                    managed_by=file_name if options["lock"] else None,
                )
                if options["lock"]:
                    # Given a first value only, like a seed.
                    Setting.objects.release(
                        *(set(constance_settings) & registry.RUNTIME_SETTINGS)
                    )

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
            # This output lands in container logs. The name heuristic also
            # hides settings declared before secret_field existed.
            if is_constance_secret(setting_key, name_heuristic=True):
                setting_value = "<redacted>"

            self.stdout.write(
                self.style.SUCCESS(
                    f"{setting_key.upper()} has been set to {setting_value}."
                )
            )
