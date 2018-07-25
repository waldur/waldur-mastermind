from celery import shared_task
import subprocess  # nosec


@shared_task(name='waldur_auth_saml2.sync_providers')
def sync_providers():
    # It is assumed that waldur console script is installed
    command = ['waldur', 'sync_saml2_providers']
    subprocess.check_output(command, stderr=subprocess.STDOUT)  # nosec
