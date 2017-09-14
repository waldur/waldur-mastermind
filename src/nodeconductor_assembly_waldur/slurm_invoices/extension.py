from __future__ import unicode_literals

from nodeconductor.core import NodeConductorExtension


class SlurmInvoicesExtension(NodeConductorExtension):

    @staticmethod
    def django_app():
        return 'nodeconductor_assembly_waldur.slurm_invoices'

    @staticmethod
    def is_assembly():
        return True
