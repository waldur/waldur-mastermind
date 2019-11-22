from waldur_mastermind.marketplace import processors

from .utils import PythonScriptMixin


class CreateProcessor(PythonScriptMixin, processors.AbstractCreateResourceProcessor):
    script_name = 'create_script'


class UpdateProcessor(PythonScriptMixin, processors.AbstractUpdateResourceProcessor):
    script_name = 'update_script'


class DeleteProcessor(PythonScriptMixin, processors.AbstractDeleteResourceProcessor):
    script_name = 'delete_script'
