"""Software the offering's catalogs provide, as lines a batch script can use.

Catalogs are linked to an offering (optionally to one partition) through
``OfferingSoftwareCatalog``; packages carry per-architecture targets. A
package version is offered on a partition when its catalog is linked to the
offering for that partition (or for all partitions) and, for an EESSI
catalog, it has a build the partition's nodes will load:

- EESSI picks one software tree per host at runtime, through the catalog's
  ``architectures_map`` (a zen5 host may use the zen4 tree), so the version
  needs a target for that tree, not just for the CPU family.
- A partition that declares a GPU is not offered builds made only for other
  GPUs; CPU-only builds still run there.
- The link's ``enabled_cpu_family`` / ``enabled_cpu_microarchitectures`` limit
  which targets count at all.

Other catalog types (Spack) carry no CPU targets and are not filtered by CPU.
"""

from dataclasses import dataclass, field

from django.db.models import Exists, OuterRef, Q

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.catalog_loaders.eessi import (
    is_eessi_catalog,
    split_eessi_cpu_arch,
)

DEFAULT_LIMIT = 20
MODULE_LOAD_PREFIXES = ("module load ", "module add ")
SPACK_LOAD_PREFIX = "spack load "


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


def catalog_setup(catalog: marketplace_models.SoftwareCatalog) -> list[str]:
    """Lines that make a catalog's modules visible in a job."""
    if is_eessi_catalog(catalog):
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


# A partition's CPU resolves to no EESSI tree at all (the map lists it with no
# compatible build), so the catalog offers nothing there.
UNSUPPORTED = object()


def _version_map(catalog) -> dict:
    """``hardware -> compatible build`` for the catalog's version."""
    architectures_map = (catalog.metadata or {}).get("architectures_map") or {}
    if not isinstance(architectures_map, dict):
        return {}
    entry = architectures_map.get(catalog.version)
    if isinstance(entry, dict):
        return {k: v for k, v in entry.items() if isinstance(k, str) and k}
    if isinstance(entry, list):
        return {k: k for k in entry if isinstance(k, str) and k}
    return {}


def eessi_build(catalog: marketplace_models.SoftwareCatalog, cpu_arch: str):
    """The ``(family, microarchitecture)`` tree EESSI loads on ``cpu_arch``.

    ``microarchitecture`` is empty when only the family is known: the
    partition names no microarchitecture, or the catalog's
    ``architectures_map`` does not list it (an older catalog, a newer CPU).
    Returns ``UNSUPPORTED`` when the map lists the CPU without a compatible
    build, and ``None`` when there is no CPU to match.
    """
    if not cpu_arch:
        return None
    family, _, rest = cpu_arch.partition("/")
    if not rest:
        return family, ""

    version_map = _version_map(catalog)
    key = cpu_arch if cpu_arch in version_map else None
    if key is None:
        # A short form such as ``x86_64/zen4`` for ``x86_64/amd/zen4``.
        candidates = [
            k
            for k in version_map
            if k.startswith(f"{family}/") and k.endswith(f"/{rest}")
        ]
        key = candidates[0] if len(candidates) == 1 else None
    if key is None:
        return family, ""
    compatible = version_map[key]
    if compatible == "":
        return UNSUPPORTED
    return split_eessi_cpu_arch(compatible if isinstance(compatible, str) else key)


def _microarchitecture_q(values) -> Q:
    """Saved restrictions may hold short tokens (``zen3``) for ``amd/zen3``."""
    q = Q(pk__in=[])
    for value in values:
        q |= Q(target_subtype=value) | Q(target_subtype__endswith=f"/{value}")
    return q


def _cpu_targets(target: Q):
    return marketplace_models.SoftwareTarget.objects.filter(
        target, target_type="cpu_architecture", version=OuterRef("pk")
    )


def _other_catalog_versions_q(catalog, partition) -> Q:
    """A version built for other CPUs only is not offered; one without CPU
    targets (Spack recipes, most hand-made catalogs) always is."""
    in_catalog = Q(package__catalog_id=catalog.pk)
    family = partition.cpu_arch.partition("/")[0] if partition is not None else ""
    if not family:
        return in_catalog
    return in_catalog & (
        ~Q(Exists(_cpu_targets(Q()))) | Q(Exists(_cpu_targets(Q(target_name=family))))
    )


def _link_versions_q(link, partition) -> Q:
    """Versions of one linked catalog that are offered on ``partition``."""
    catalog = link.catalog
    if not is_eessi_catalog(catalog):
        return _other_catalog_versions_q(catalog, partition)

    in_catalog = Q(package__catalog_id=catalog.pk)
    target = Q()
    restricted = False
    if link.enabled_cpu_family:
        target &= Q(target_name__in=link.enabled_cpu_family)
        restricted = True
    if link.enabled_cpu_microarchitectures:
        target &= _microarchitecture_q(link.enabled_cpu_microarchitectures)
        restricted = True

    if partition is not None:
        build = eessi_build(catalog, partition.cpu_arch)
        if build is UNSUPPORTED:
            return Q(pk__in=[])
        if build is not None:
            family, microarchitecture = build
            target &= Q(target_name=family)
            if microarchitecture:
                # A target without a microarchitecture is a family-wide build.
                target &= _microarchitecture_q([microarchitecture]) | Q(
                    target_subtype=""
                )
            restricted = True
        if partition.gpu_arch:
            target &= Q(gpu_architectures=[]) | Q(
                gpu_architectures__contains=[partition.gpu_arch]
            )
            restricted = True

    if not restricted:
        return in_catalog
    return in_catalog & Q(Exists(_cpu_targets(target)))


def _links(offering, partition=None):
    links = marketplace_models.OfferingSoftwareCatalog.objects.filter(
        offering=offering
    ).select_related("catalog")
    if partition is not None:
        links = links.filter(Q(partition__isnull=True) | Q(partition=partition))
    return list(links)


def _offered_versions(links, partition):
    if not links:
        return marketplace_models.SoftwareVersion.objects.none()
    offered = Q(pk__in=[])
    for link in links:
        offered |= _link_versions_q(link, partition)
    return marketplace_models.SoftwareVersion.objects.filter(offered).select_related(
        "package__catalog"
    )


def offered_versions(offering, partition=None):
    """Every software version the offering's catalogs offer on ``partition``."""
    return _offered_versions(_links(offering, partition), partition)


def _setup_lines(links, partition) -> set[str]:
    lines = set()
    for link in links:
        if partition is not None and is_eessi_catalog(link.catalog):
            if eessi_build(link.catalog, partition.cpu_arch) is UNSUPPORTED:
                continue
        lines.update(catalog_setup(link.catalog))
    return lines


def offered_setup_lines(offering, partition=None) -> set[str]:
    """Catalog setup lines (``module load EESSI/<ver>``) usable on ``partition``."""
    return _setup_lines(_links(offering, partition), partition)


def find_software(
    offering, query="", partition=None, limit=DEFAULT_LIMIT
) -> list[SoftwareMatch]:
    versions = offered_versions(offering, partition)
    if query:
        versions = versions.filter(
            Q(package__name__icontains=query) | Q(package__description__icontains=query)
        )
    versions = versions.prefetch_related("targets").order_by("package__name", "-id")
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


def requested_loads(modules: list[str]) -> list[str]:
    """The load lines a job's setup asks for, one per module or spec.

    ``module load A B`` becomes ``module load A`` and ``module load B``;
    options (``-q``) are dropped; ``module purge``, ``module use`` and
    ``source`` lines are not loads.
    """
    loads = []
    for line in modules:
        line = " ".join(line.split())
        for prefix, as_prefix in (
            *((p, "module load ") for p in MODULE_LOAD_PREFIXES),
            (SPACK_LOAD_PREFIX, SPACK_LOAD_PREFIX),
        ):
            if line.startswith(prefix):
                loads += [
                    f"{as_prefix}{item}"
                    for item in line[len(prefix) :].split()
                    if not item.startswith("-")
                ]
                break
    return loads


def _module_satisfied(requested: str, offered: str) -> bool:
    """``GROMACS`` and ``GROMACS/2024.1`` both name ``GROMACS/2024.1-foss-2023b``."""
    return offered == requested or any(
        offered.startswith(requested + sep) for sep in ("/", "-", ".")
    )


def _describe(version) -> tuple[str, str, str]:
    """``(kind, name, rest)`` of a version's load line, lower case.

    For a module ``rest`` is the full module (``gromacs/2024.1-foss-2023b``),
    for a Spack spec the version.
    """
    line = load_line(version).lower()
    if line.startswith(SPACK_LOAD_PREFIX):
        name, _, number = line[len(SPACK_LOAD_PREFIX) :].partition("@")
        return "spack", name, number
    module = line[len("module load ") :]
    return "module", module.split("/", 1)[0], module


def unoffered_loads(offering, partition, loads: list[str]) -> list[str]:
    """Loads of catalog software that the catalogs do not offer on ``partition``.

    A module the linked catalogs do not know at all (``cray-mpich``, a site
    compiler) is the site's business and is not reported; a misspelt name
    cannot be told apart from one.
    """
    links = _links(offering) if loads else []
    if not links:
        return []
    partition_links = [
        link
        for link in links
        if partition is None or link.partition_id in (None, partition.pk)
    ]
    setup = {line.lower() for line in _setup_lines(partition_links, partition)}

    requests = []  # (line, kind, name, rest), lower case like _describe
    for line in loads:
        lowered = line.lower()
        if lowered.startswith(SPACK_LOAD_PREFIX):
            name, _, number = lowered[len(SPACK_LOAD_PREFIX) :].partition("@")
            requests.append((line, "spack", name, number))
        else:
            module = lowered[len("module load ") :]
            requests.append((line, "module", module.split("/", 1)[0], module))

    named = Q(pk__in=[])
    for name in {name for _, _, name, _ in requests if name != "eessi"}:
        named |= Q(package__name__iexact=name) | Q(
            metadata__module__full_module_name__istartswith=f"{name}/"
        )
    known = set()
    for version in marketplace_models.SoftwareVersion.objects.filter(
        named, package__catalog_id__in=[link.catalog_id for link in links]
    ).select_related("package__catalog"):
        kind, name, _ = _describe(version)
        known |= {(kind, name), (kind, version.package.name.lower())}
    offered = [
        _describe(version)
        for version in _offered_versions(partition_links, partition).filter(named)
    ]

    missing = []
    for line, kind, name, rest in requests:
        if kind == "module" and name == "eessi":
            if line.lower() not in setup:
                missing.append(line)
        elif (kind, name) not in known:
            continue
        elif kind == "spack":
            if not any(
                o_kind == "spack" and o_name == name and rest in ("", o_rest)
                for o_kind, o_name, o_rest in offered
            ):
                missing.append(line)
        elif not any(
            o_kind == "module" and _module_satisfied(rest, o_rest)
            for o_kind, _, o_rest in offered
        ):
            missing.append(line)
    return missing


def _version_key(version: str):
    """Sort newest first: invert numeric parts, keep text parts comparable."""
    parts = []
    for part in version.replace("-", ".").split("."):
        if part.isdigit():
            parts.append((0, -int(part), ""))
        else:
            parts.append((1, 0, part))
    return parts
