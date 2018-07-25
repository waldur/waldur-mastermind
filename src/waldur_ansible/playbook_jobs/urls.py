from waldur_ansible.playbook_jobs import models, views


def register_in(router):
    router.register(r'ansible-playbooks', views.PlaybookViewSet, base_name=models.Playbook.get_url_name())
    router.register(r'ansible-jobs', views.JobViewSet, base_name=models.Job.get_url_name())
