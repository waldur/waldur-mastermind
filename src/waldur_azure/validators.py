"""
See also:
https://docs.microsoft.com/en-us/azure/architecture/best-practices/naming-conventions
https://docs.microsoft.com/en-us/azure/virtual-machines/linux/faq
"""

import re

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator, MinLengthValidator, MaxLengthValidator
from django.utils.translation import ugettext_lazy as _


ResourceGroupNameValidator = RegexValidator(
    regex=re.compile(r'^[-\w._()]+$'),
    message=_('The name can include alphanumeric, underscore, parentheses, '
              'hyphen, period (except at end), and Unicode characters '
              'that match the allowed characters.')
)


VirtualMachineNameValidator = RegexValidator(
    regex=re.compile(r'[a-zA-Z][a-zA-Z0-9-]{0,13}[a-zA-Z0-9]$'),
    message=_('The name can contain only letters, numbers, and hyphens. '
              'The name must be shorter than 15 characters and start with '
              'a letter and must end with a letter or a number.')
)


BLACKLIST_USERNAMES = (
    'administrator',
    'admin',
    'user',
    'user1',
    'test',
    'user2',
    'test1',
    'user3',
    'admin1',
    '1',
    '123',
    'a',
    'actuser',
    'adm',
    'admin2',
    'aspnet',
    'backup',
    'console',
    'david',
    'guest',
    'john',
    'owner',
    'root',
    'server',
    'sql',
    'support',
    'support_388945a0',
    'sys',
    'test2',
    'test3',
    'user4',
    'user5',
)


def validate_username(username):
    if username.lower() in BLACKLIST_USERNAMES:
        raise ValidationError(_('Username is blacklisted.'))


BLACKLIST_PASSWORDS = (
    'abc@123',
    'P@$$w0rd',
    'P@ssw0rd',
    'P@ssword123',
    'Pa$$word',
    'pass@word1',
    'Password!',
    'Password1',
    'Password22',
    'iloveyou!',
)


def validate_password(password):
    if password in BLACKLIST_PASSWORDS:
        raise ValidationError(_('Password is blacklisted.'))

    groups = (r'[a-z]', r'[A-Z]', r'[0-9]', r'[\W_]')
    if sum(bool(re.search(g, password)) for g in groups) < 3:
        raise ValidationError(_('The supplied password must contain 3 of the following: '
                                'a lowercase character, an uppercase character, a number, '
                                'a special character.'))


VirtualMachinePasswordValidators = [
    MinLengthValidator(6),
    MaxLengthValidator(72),
    validate_password,
]


NetworkingNameValidator = RegexValidator(
    regex=re.compile(r'[a-zA-Z][a-zA-Z0-9._-]+$'),
    message=_('The name can contain only letters, numbers, underscore, period and hyphens.')
)


StorageAccountNameValidator = RegexValidator(
    regex=re.compile(r'[a-z][a-z0-9]{2,23}$'),
    message=_('The name can contain only letters and numbers.')
)
