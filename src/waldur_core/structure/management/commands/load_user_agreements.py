from django.core.management.base import BaseCommand

from waldur_core.structure.models import UserAgreement


class Command(BaseCommand):
    help = "Imports privacy policy and terms of service into DB"

    def create_user_agreement(self, filepath, agreement_type, language="", force=False):
        try:
            user_agreement_count = UserAgreement.objects.filter(
                agreement_type=agreement_type, language=language
            ).count()
            if not force and user_agreement_count > 0:
                lang_info = f" ({language})" if language else " (default)"
                self.stdout.write(
                    self.style.NOTICE(
                        f"The {agreement_type}{lang_info} agreement already exists, skipping loading"
                    )
                )
                return

            with open(filepath) as agreement_file:
                content = agreement_file.read()

            UserAgreement.objects.update_or_create(
                agreement_type=agreement_type,
                language=language,
                defaults={"content": content},
            )
            lang_info = f" ({language})" if language else " (default)"
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully loaded {agreement_type}{lang_info} agreement"
                )
            )
        except Exception as e:
            return e

    def add_arguments(self, parser):
        parser.add_argument(
            "-tos",
            "--tos",
            type=str,
            help="Path to a Terms of service file",
            required=False,
        )
        parser.add_argument(
            "-pp",
            "--pp",
            type=str,
            help="Path to a Privacy policy file",
            required=False,
        )
        parser.add_argument(
            "-l",
            "--language",
            type=str,
            default="",
            help="ISO 639-1 language code (e.g., 'en', 'de', 'et'). "
            "Leave empty for the default version.",
        )
        parser.add_argument(
            "-f",
            "--force",
            dest="force",
            action="store_true",
            default=False,
            help="Force loading agreements even if they are already defined in DB.",
        )

    def handle(self, *args, **options):
        tos_path = options.get("tos")
        pp_path = options.get("pp")
        language = options.get("language", "")
        force = options.get("force")

        if not tos_path and not pp_path:
            self.stdout.write(
                self.style.ERROR(
                    "You must specify a path to ToS or Privacy Policy files to create them."
                )
            )
            return

        if tos_path:
            try:
                self.create_user_agreement(
                    tos_path, UserAgreement.UserAgreements.TOS, language, force
                )
            except Exception:
                self.stdout.write(self.style.ERROR("Couldn't create Terms of Service"))
                return

        if pp_path:
            try:
                self.create_user_agreement(
                    pp_path, UserAgreement.UserAgreements.PP, language, force
                )
            except Exception:
                self.stdout.write(self.style.ERROR("Couldn't create Privacy policy"))
                return
