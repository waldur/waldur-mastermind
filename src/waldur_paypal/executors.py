from celery import chain

from waldur_core.core import executors as core_executors, tasks as core_tasks


class InvoiceCreateExecutor(core_executors.BaseExecutor):

    @classmethod
    def get_task_signature(cls, invoice, serialized_invoice, **kwargs):
        tasks = [
            core_tasks.BackendMethodTask().si(serialized_invoice, 'create_invoice'),
            core_tasks.BackendMethodTask().si(serialized_invoice, 'download_invoice_pdf'),
            core_tasks.BackendMethodTask().si(serialized_invoice, 'send_invoice'),
            core_tasks.BackendMethodTask().si(serialized_invoice, 'pull_invoice'),
        ]

        return chain(*tasks)


class InvoicePullExecutor(core_executors.BaseExecutor):

    @classmethod
    def get_task_signature(cls, invoice, serialized_invoice, **kwargs):
        tasks = [
            core_tasks.BackendMethodTask().si(serialized_invoice, 'pull_invoice'),
            core_tasks.BackendMethodTask().si(serialized_invoice, 'download_invoice_pdf'),
        ]

        return chain(*tasks)
