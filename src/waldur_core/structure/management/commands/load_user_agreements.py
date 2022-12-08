from django.core.management.base import BaseCommand

from waldur_core.structure.models import UserAgreement


class Command(BaseCommand):
    help = 'Imports privacy policy and terms of service into DB'

    def create_user_agreement(self, filepath, agreement_type, force=False):
        try:
            user_agreement_count = UserAgreement.objects.filter(
                agreement_type=agreement_type
            ).count()
            if not force and user_agreement_count > 0:
                self.stdout.write(
                    self.style.NOTICE(
                        'The %s agreement already exists, skipping loading'
                        % agreement_type,
                    )
                )
                return

            with open(filepath, 'r') as agreement_file:
                content = agreement_file.read()

            UserAgreement.objects.update_or_create(
                agreement_type=agreement_type, defaults={'content': content}
            )
        except Exception as e:
            return e

    def add_arguments(self, parser):
        parser.add_argument(
            '-tos',
            '--tos',
            type=str,
            help='Path to a Terms of service file',
            required=False,
        )
        parser.add_argument(
            '-pp',
            '--pp',
            type=str,
            help='Path to a Privacy policy file',
            required=False,
        )
        parser.add_argument(
            '-f',
            '--force',
            dest='force',
            default=False,
            help='This flag means force loading agreements even if they are already defined in DB.',
        )

    def handle(self, *args, **options):
        tos_path = options.get('tos')
        pp_path = options.get('pp')
        force = options.get('force')

        if not tos_path and not pp_path:
            self.stdout.write(
                self.style.ERROR(
                    'You must specify a path to ToS or Privacy Policy files to create them.'
                )
            )
            return

        if tos_path:
            try:
                self.create_user_agreement(
                    tos_path, UserAgreement.UserAgreements.TOS, force
                )
            except Exception:
                self.stdout.write(self.style.ERROR("Couldn't create Terms of Service"))
                return

        if pp_path:
            try:
                self.create_user_agreement(
                    pp_path, UserAgreement.UserAgreements.PP, force
                )
            except Exception:
                self.stdout.write(self.style.ERROR("Couldn't create Privacy policy"))
                return
