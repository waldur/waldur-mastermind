from django.core.management.base import BaseCommand

from waldur_core.structure.models import UserAgreement


def create_user_agreement(filepath, agreement_type):
    try:
        with open(filepath, 'r') as agreement_file:
            content = agreement_file.read()
        UserAgreement.objects.update_or_create(
            agreement_type=agreement_type, defaults={'content': content}
        )
    except Exception as e:
        return e


class Command(BaseCommand):
    help = 'Imports privacy policy and terms of service into DB'

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

    def handle(self, *args, **options):
        tos_path = options.get('tos')
        pp_path = options.get('pp')

        if not tos_path and not pp_path:
            self.stdout.write(
                self.style.ERROR(
                    'You must specify a path to ToS or Privacy Policy files to create them.'
                )
            )
            return

        if tos_path:
            try:
                create_user_agreement(tos_path, UserAgreement.UserAgreements.TOS)
            except Exception:
                self.stdout.write(self.style.ERROR("Couldn't create Terms of Service"))
                return

        if pp_path:
            try:
                create_user_agreement(pp_path, UserAgreement.UserAgreements.PP)
            except Exception:
                self.stdout.write(self.style.ERROR("Couldn't create Privacy policy"))
                return
