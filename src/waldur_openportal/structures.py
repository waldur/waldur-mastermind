import collections

Account = collections.namedtuple("Account", ["name", "description", "organization"])
Association = collections.namedtuple("Association", ["account", "user", "value"])
