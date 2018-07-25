import collections

Account = collections.namedtuple('Account', ['name', 'description', 'organization'])
Association = collections.namedtuple('Association', ['account', 'user', 'value'])


class Quotas(object):
    def __init__(self, cpu=0, gpu=0, ram=0, deposit=0):
        self.cpu = cpu
        self.gpu = gpu
        self.ram = ram
        self.deposit = deposit

    def __add__(self, other):
        return Quotas(
            self.cpu + other.cpu,
            self.gpu + other.gpu,
            self.ram + other.ram,
            self.deposit + other.deposit,
        )
