from __future__ import unicode_literals

import re
import six


def snake_to_camel(word):
    return ''.join(x.capitalize() or '_' for x in word.split('_'))


def camel_to_snake(word):
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', word)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()


def hstore_to_dict(hsore):
    """
    :param hsore: JSON string. For example:
        {
          "cloudDeploymentModel__private_cloud": true,
          "vendorType__reseller": true,
          "userSupportOptions__web_chat": true,
          "userSupportOptions__phone": true,
          "dataProtectionInternal__ipsec": true,
          "dataProtectionExternal__tls12": true
        }
    :return: JSON string. For example:
        {
          "cloudDeploymentModel": "private_cloud",
          "vendorType": "reseller",
          "userSupportOptions": ["web_chat", "phone"],
          "dataProtectionInternal": "ipsec",
          "dataProtectionExternal": "tls12"
        }
    """
    attributes = {}
    for attr in hsore:
        attr_list = attr.split('__')
        key = attr_list[0]
        if len(attr_list) > 1:
            if key in attributes:
                value = attributes[key]
                if isinstance(value, list):
                    attributes[key].append(attr_list[1])
                else:
                    attributes[key] = [value, attr_list[1]]
            else:
                attributes[key] = attr_list[1]
        else:
            attributes[attr] = hsore[attr]
    return attributes


def dict_to_hstore(dictionary):
    result = {}
    for key, value in dictionary.items():
        if isinstance(value, int):
            result[key] = value

        if isinstance(value, six.text_type) and re.match("^[A-Za-z0-9-]+$", value):
            result[key + '__' + value] = True

        if isinstance(value, list) and value:
            for v in value:
                if isinstance(v, six.text_type) and re.match("^[A-Za-z0-9-]+$", v):
                    result[key + '__' + v] = True
    return result
