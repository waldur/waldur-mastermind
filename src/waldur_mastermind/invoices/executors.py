from waldur_core.core import executors as core_executors

from . import tasks


class InvoicePDFCreateExecutor(core_executors.BaseExecutor):

    @classmethod
    def get_task_signature(cls, invoice, serialized_invoice, **kwargs):
        return tasks.create_invoice_pdf.si(serialized_invoice)
