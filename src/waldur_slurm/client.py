import logging
import re

from waldur_slurm.base import BatchError, BaseBatchClient
from waldur_slurm.parser import SlurmReportLine
from waldur_slurm.structures import Account, Association
from waldur_slurm.utils import format_current_month


class SlurmError(BatchError):
    pass


logger = logging.getLogger(__name__)


class SlurmClient(BaseBatchClient):
    """
    This class implements Python client for SLURM.
    See also: https://slurm.schedmd.com/sacctmgr.html
    """

    def list_accounts(self):
        output = self._execute_command(['list', 'account'])
        return [self._parse_account(line) for line in output.splitlines() if '|' in line]

    def _parse_account(self, line):
        parts = line.split('|')
        return Account(
            name=parts[0],
            description=parts[1],
            organization=parts[2],
        )

    def get_account(self, name):
        output = self._execute_command(['show', 'account', name])
        lines = [line for line in output.splitlines() if '|' in line]
        if len(lines) == 0:
            return None
        return self._parse_account(lines[0])

    def create_account(self, name, description, organization, parent_name=None):
        parts = [
            'add', 'account', name,
            'description="%s"' % description,
            'organization="%s"' % organization,
        ]
        if parent_name:
            parts.append('parent=%s' % parent_name)
        return self._execute_command(parts)

    def delete_all_users_from_account(self, name):
        return self._execute_command(['remove', 'user', 'where', 'account=%s' % name])

    def account_has_users(self, account):
        output = self._execute_command([
            'show', 'association', 'where', 'account=%s' % account
        ])
        items = [self._parse_association(line) for line in output.splitlines() if '|' in line]
        return any(item.user != '' for item in items)

    def delete_account(self, name):
        if self.account_has_users(name):
            self.delete_all_users_from_account(name)

        return self._execute_command(['remove', 'account', 'where', 'name=%s' % name])

    def set_resource_limits(self, account, quotas):
        quota = 'GrpTRESMins=cpu=%d,gres/gpu=%d,mem=%d' % (quotas.cpu, quotas.gpu, quotas.ram)
        return self._execute_command(['modify', 'account', account, 'set', quota])

    def get_association(self, user, account):
        output = self._execute_command([
            'show', 'association', 'where', 'user=%s' % user, 'account=%s' % account
        ])
        lines = [line for line in output.splitlines() if '|' in line]
        if len(lines) == 0:
            return None
        return self._parse_association(lines[0])

    def _parse_association(self, line):
        parts = line.split('|')
        value = parts[9]
        match = re.match(r'cpu=(\d+)', value)
        if match:
            value = int(match.group(1))
        return Association(
            account=parts[1],
            user=parts[2],
            value=value,
        )

    def create_association(self, username, account, default_account=''):
        return self._execute_command(['add', 'user', username,
                                      'account=%s' % account,
                                      'DefaultAccount=%s' % default_account])

    def delete_association(self, username, account):
        return self._execute_command([
            'remove', 'user', 'where', 'name=%s' % username, 'and', 'account=%s' % account
        ])

    def get_usage_report(self, accounts):
        month_start, month_end = format_current_month()

        args = [
            '--noconvert',
            '--truncate',
            '--allocations',
            '--allusers',
            '--starttime=%s' % month_start,
            '--endtime=%s' % month_end,
            '--accounts=%s' % ','.join(accounts),
            '--format=Account,ReqTRES,Elapsed,User',
        ]
        output = self._execute_command(args, 'sacct', immediate=False)
        return [SlurmReportLine(line) for line in output.splitlines() if '|' in line]

    def _execute_command(self, command, command_name='sacctmgr', immediate=True):
        account_command = [command_name, '--parsable2', '--noheader']
        if immediate:
            account_command.append('--immediate')
        account_command.extend(command)
        return self.execute_command(account_command)
