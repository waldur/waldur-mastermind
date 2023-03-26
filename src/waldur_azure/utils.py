import hashlib
import string

from django.utils.crypto import get_random_string

from waldur_core.core import utils as core_utils


def hash_string(value, length=16):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:length]


def generate_username():
    return f'user{core_utils.pwgen(4)}'


def generate_password():
    lowercase = get_random_string(5, string.ascii_lowercase)
    uppercase = get_random_string(5, string.ascii_uppercase)
    digits = get_random_string(5, string.digits)
    return lowercase + uppercase + digits
