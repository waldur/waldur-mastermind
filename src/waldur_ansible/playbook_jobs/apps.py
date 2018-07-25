from django.apps import AppConfig
from django.db.models import signals


class PlaybookJobsConfig(AppConfig):
    name = 'waldur_ansible.playbook_jobs'
    verbose_name = 'Waldur Ansible Playbooks'

    def ready(self):
        from . import handlers

        Playbook = self.get_model('Playbook')

        signals.pre_delete.connect(
            handlers.delete_playbook_workspace,
            sender=Playbook,
            dispatch_uid='waldur_ansible.handlers.delete_playbook_workspace',
        )

        signals.pre_save.connect(
            handlers.resize_playbook_image,
            sender=Playbook,
            dispatch_uid='waldur_ansible.handlers.resize_playbook_image',
        )
