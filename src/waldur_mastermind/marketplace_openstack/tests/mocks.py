from dataclasses import asdict, dataclass


@dataclass
class MockInstance:
    name: str
    id: str
    status: str
    key_name: str
    created: str
    flavor: dict
    image: dict
    networks: dict

    def to_dict(self):
        return asdict(self)


@dataclass
class MockFlavor:
    name: str
    vcpus: int
    disk: int
    ram: int


class MockTenant:
    def __init__(self, name, id=None, description=""):
        self.name = name
        self.id = id
        self.description = description


@dataclass
class MockVolume:
    name: str
    id: str
    size: int
    volume_type = ""
    description = ""
    status = "available"
    bootable = "false"
    metadata = {}


@dataclass
class MockImage:
    name: str
    id: str


@dataclass
class MockLimits:
    ram: int
    cores: int
    gigabytes: int
    _info: dict


MOCK_INSTANCE = MockInstance(
    name="VM-1",
    id="1",
    status="active",
    key_name="ssh-public",
    created="2020-02-02T02:02",
    flavor={"id": "std"},
    image={"id": "image_id"},
    networks={
        "test-int-net": ["192.168.42.60"],
        "public": ["172.29.249.185"],
    },
)


MOCK_FLAVOR = MockFlavor(name="Standard", vcpus=4, disk=100, ram=4096)

MOCK_VOLUME = MockVolume(name="ssd-volume", id="1", size=100)

MOCK_TENANT = MockTenant(name="admin", id="1", description="admin")

MOCK_IMAGE = MockImage(
    name="ubuntu",
    id="1",
)

MOCK_LIMITS = MockLimits(
    cores=2,
    ram=1024,
    gigabytes=10,
    _info={
        "gigabytes_high-iops": -1,
        "gigabytes_low-iops": -1,
        "gigabytes_lvm": -1,
        "gigabytes_ultra-high-iops": -1,
    },
)
