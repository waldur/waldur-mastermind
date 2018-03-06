import math

from waldur_mastermind.invoices import registrators
from waldur_slurm.structures import Quotas


def get_price(quotas, package):
    minutes_in_hour = 60
    mb_in_gb = 1024
    cpu_price = int(math.ceil(1.0 * quotas.cpu / minutes_in_hour)) * package.cpu_price
    gpu_price = int(math.ceil(1.0 * quotas.gpu / minutes_in_hour)) * package.gpu_price
    ram_price = int(math.ceil(1.0 * quotas.ram / mb_in_gb)) * package.ram_price
    return cpu_price + gpu_price + ram_price


def get_package(allocation):
    registrator = registrators.RegistrationManager.get_registrator(allocation)
    return registrator.get_package(allocation)


def get_deposit_limit(allocation, package):
    quotas = Quotas(allocation.cpu_limit, allocation.gpu_limit, allocation.ram_limit)
    return get_price(quotas, package)


def get_deposit_usage(allocation, package):
    if allocation.batch_service == 'MOAB':
        return allocation.deposit_usage
    else:
        quotas = Quotas(allocation.cpu_usage, allocation.gpu_usage, allocation.ram_usage)
        return get_price(quotas, package)
