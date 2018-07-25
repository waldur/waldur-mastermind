from __future__ import absolute_import

import abc
import logging
import subprocess  # nosec

from django.utils.functional import cached_property
import six

from .structures import Quotas


logger = logging.getLogger(__name__)


class BatchError(Exception):
    pass


@six.add_metaclass(abc.ABCMeta)
class BaseBatchClient(object):

    def __init__(self, hostname, key_path, username='root', port=22, use_sudo=False):
        self.hostname = hostname
        self.key_path = key_path
        self.username = username
        self.port = port
        self.use_sudo = use_sudo

    @abc.abstractmethod
    def list_accounts(self):
        """
        Get accounts list.
        :return: list[structures.Account object]
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def get_account(self, name):
        """
        Get account info.
        :param name: [string] batch account name
        :return: [structures.Account object]
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def create_account(self, name, description, organization, parent_name=None):
        """
        Create account.
        :param name: [string] account name
        :param description: [string] account description
        :param organization: [string] account organization name
        :param parent_name: [string] account parent name. Optional.
        :return: None
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def delete_account(self, name):
        """
        Delete account.
        :param name: [string] account name
        :return: None
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def set_resource_limits(self, account, quotas):
        """
        Set account limits.
        :param account: [string] account name
        :param quotas: [structures.Quotas object] limits
        :return: None
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def get_association(self, user, account):
        """
        Get association user and account.
        :param user: [string] user name
        :param account: [string] account name
        :return: [structures.Association object]
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def create_association(self, username, account, default_account=None):
        """
        Create association user and account
        :param username: [string] user name
        :param account: [string] account name
        :param default_account: [string] default account name. Optional.
        :return: None
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def delete_association(self, username, account):
        """
        Delete_association user and account.
        :param username: [string] user name
        :param account: [string] account name
        :return: None
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def get_usage_report(self, accounts):
        """
        Get usages records.
        :param accounts: list[string]
        :return: list[BaseReportLine]
        """
        raise NotImplementedError()

    def execute_command(self, command):
        server = '%s@%s' % (self.username, self.hostname)
        port = str(self.port)
        if self.use_sudo:
            account_command = ['sudo']
        else:
            account_command = []

        account_command.extend(command)
        ssh_command = ['ssh', '-o', 'UserKnownHostsFile=/dev/null', '-o', 'StrictHostKeyChecking=no',
                       server, '-p', port, '-i', self.key_path, ' '.join(account_command)]
        try:
            logger.debug('Executing SSH command: %s', ' '.join(ssh_command))
            return subprocess.check_output(ssh_command, stderr=subprocess.STDOUT)  # nosec
        except subprocess.CalledProcessError as e:
            logger.exception('Failed to execute command "%s".', ssh_command)
            stdout = e.output or ''
            lines = stdout.splitlines()
            if len(lines) > 0 and lines[0].startswith('Warning: Permanently added'):
                lines = lines[1:]
            stdout = '\n'.join(lines)
            six.reraise(BatchError, stdout)


@six.add_metaclass(abc.ABCMeta)
class BaseReportLine(object):
    @abc.abstractproperty
    def account(self):
        pass

    @abc.abstractproperty
    def user(self):
        pass

    @property
    def cpu(self):
        return 0

    @property
    def gpu(self):
        return 0

    @property
    def ram(self):
        return 0

    @property
    def duration(self):
        return 0

    @property
    def charge(self):
        return 0

    @property
    def node(self):
        return 0

    @cached_property
    def quotas(self):
        return Quotas(
            self.cpu * self.duration * self.node,
            self.gpu * self.duration * self.node,
            self.ram * self.duration * self.node,
            self.charge
        )
