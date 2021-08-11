from waldur_core.core.models import RuntimeStateMixin
from waldur_core.structure.models import BaseResource


class Job(BaseResource, RuntimeStateMixin):
    @classmethod
    def get_url_name(cls):
        return 'slurm-job'
