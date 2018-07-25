import logging

from waldur_slurm.base import BatchError, BaseBatchClient
from waldur_slurm.parser_moab import MoabReportLine
from waldur_slurm.structures import Account, Association
from waldur_slurm.utils import format_current_month


class MoabError(BatchError):
    pass


logger = logging.getLogger(__name__)


class MoabClient(BaseBatchClient):
    """
    This class implements Python client for MOAB.
    See also MOAB Accounting Manager 9.1.1 Administrator Guide
    http://docs.adaptivecomputing.com/9-1-1/MAM/help.htm"""

    def list_accounts(self):
        output = self.execute_command(
            'mam-list-accounts --raw --quiet --show Name,Description,Organization'.split()
        )
        return [self._parse_account(line) for line in output.splitlines() if '|' in line]

    def _parse_account(self, line):
        parts = line.split('|')
        return Account(
            name=parts[0],
            description=parts[1],
            organization=parts[2],
        )

    def get_account(self, name):
        command = 'mam-list-accounts --raw --quiet --show Name,Description,Organization -a %s' % name
        output = self.execute_command(command.split())
        lines = [line for line in output.splitlines() if '|' in line]
        if len(lines) == 0:
            return None
        return self._parse_account(lines[0])

    def create_account(self, name, description, organization, parent_name=None):
        command = 'mam-create-account -a %(name)s -d "%(description)s" -o %(organization)s' % {
            'name': name,
            'description': description,
            'organization': organization,
        }
        return self.execute_command(command.split())

    def delete_account(self, name):
        command = 'mam-delete-account -a %s' % name
        return self.execute_command(command.split())

    def set_resource_limits(self, account, quotas):
        if quotas.deposit < 0:
            logger.warning('Skipping limit update because pricing '
                           'package is not created for the related service settings.')
            return
        command = 'mam-deposit -a %(account)s -z %(deposit_amount)s --create-fund True' % {
            'account': account,
            'deposit_amount': quotas.deposit
        }
        return self.execute_command(command.split())

    def get_association(self, user, account):
        command = 'mam-list-funds --raw --quiet -u %(user)s -a %(account)s --show Constraints,Balance' % \
                  {'user': user, 'account': account}
        output = self.execute_command(command.split())
        lines = [line for line in output.splitlines() if '|' in line]
        if len(lines) == 0:
            return None

        return Association(
            account=account,
            user=user,
            value=lines[0].split('|')[-1],
        )

    def create_association(self, username, account, default_account=None):
        command = 'mam-modify-account --add-user %(username)s -a %(account)s' % {
            'username': username,
            'account': account
        }
        return self.execute_command(command.split())

    def delete_association(self, username, account):
        command = 'mam-modify-account --del-user %(username)s -a %(account)s' % {
            'username': username,
            'account': account
        }
        return self.execute_command(command.split())

    def get_usage_report(self, accounts):
        template = (
            'mam-list-usagerecords --raw --quiet --show '
            'Account,Processors,GPUs,Memory,Duration,User,Charge,Nodes '
            '-a %(account)s -s %(start)s -e %(end)s'
        )
        month_start, month_end = format_current_month()

        report_lines = []
        for account in accounts:
            command = template % {
                'account': account,
                'start': month_start,
                'end': month_end,
            }
            lines = self.execute_command(command.split()).splitlines()
            for line in lines:
                if '|' in line:
                    report_lines.append(MoabReportLine(line))

        return report_lines
