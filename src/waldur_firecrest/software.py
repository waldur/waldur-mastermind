"""Software the offering's catalogs provide, as lines a batch script can use.

Catalogs are linked to an offering (optionally to one partition) through
``OfferingSoftwareCatalog``; packages carry per-architecture targets. A
package is offered on a partition when its catalog is linked to the offering
for that partition (or for all partitions) and, if the partition declares a
CPU architecture, one of its targets is built for that CPU family.
"""

from dataclasses import dataclass, field

from django.db.models import Q

from waldur_mastermind.marketplace import models as marketplace_models

DEFAULT_LIMIT = 20


@dataclass
class SoftwareMatch:
    name: str
    version: str
    catalog: str
    catalog_type: str
    description: str
    setup: list[str] = field(default_factory=list)
    load: str = ""
    cpu_families: list[str] = field(default_factory=list)
    gpu_architectures: list[str] = field(default_factory=list)


def cpu_family(cpu_arch: str) -> str:
    """``x86_64/amd/zen3`` -> ``x86_64``."""
    return cpu_arch.split("/", 1)[0] if cpu_arch else ""


def catalog_setup(catalog: marketplace_models.SoftwareCatalog) -> list[str]:
    """Lines that make a catalog's modules visible in a job."""
    if catalog.name.upper() == "EESSI":
        return [f"module load EESSI/{catalog.version}"]
    return []


def load_line(version: marketplace_models.SoftwareVersion) -> str:
    package = version.package
    if package.catalog.catalog_type == "source_package":
        return f"spack load {package.name}@{version.version}"
    module = (version.metadata or {}).get("module") or {}
    if module.get("full_module_name"):
        return f"module load {module['full_module_name']}"
    return f"module load {package.name}/{version.module_version or version.version}"


def find_software(
    offering, query="", partition=None, limit=DEFAULT_LIMIT
) -> list[SoftwareMatch]:
    links = marketplace_models.OfferingSoftwareCatalog.objects.filter(offering=offering)
    if partition is not None:
        links = links.filter(Q(partition__isnull=True) | Q(partition=partition))
    catalog_ids = list(links.values_list("catalog_id", flat=True))
    if not catalog_ids:
        return []

    versions = marketplace_models.SoftwareVersion.objects.filter(
        package__catalog_id__in=catalog_ids
    ).select_related("package__catalog")
    if query:
        versions = versions.filter(
            Q(package__name__icontains=query) | Q(package__description__icontains=query)
        )
    family = cpu_family(partition.cpu_arch) if partition is not None else ""
    if family:
        versions = versions.filter(
            targets__target_type="cpu_architecture", targets__target_name=family
        )
    versions = (
        versions.prefetch_related("targets").distinct().order_by("package__name", "-id")
    )
    # Exact name matches first, then the newest versions of each package.
    versions = sorted(
        versions[: limit * 5],
        key=lambda v: (
            v.package.name.lower() != (query or "").lower(),
            v.package.name.lower(),
            _version_key(v.version),
        ),
    )

    matches = []
    for version in versions[:limit]:
        package = version.package
        targets = list(version.targets.all())
        matches.append(
            SoftwareMatch(
                name=package.name,
                version=version.version,
                catalog=f"{package.catalog.name} {package.catalog.version}",
                catalog_type=package.catalog.catalog_type,
                description=(package.description or "")[:300],
                setup=catalog_setup(package.catalog),
                load=load_line(version),
                cpu_families=sorted(
                    {
                        t.target_name
                        for t in targets
                        if t.target_type == "cpu_architecture"
                    }
                ),
                gpu_architectures=sorted(
                    {gpu for t in targets for gpu in (t.gpu_architectures or [])}
                ),
            )
        )
    return matches


def _version_key(version: str):
    """Sort newest first: invert numeric parts, keep text parts comparable."""
    parts = []
    for part in version.replace("-", ".").split("."):
        if part.isdigit():
            parts.append((0, -int(part), ""))
        else:
            parts.append((1, 0, part))
    return parts
