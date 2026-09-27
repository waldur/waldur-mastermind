#!/usr/bin/env python3
"""Generate the ``public_sector_accounting`` demo preset.

A public-sector cloud operated for government bodies, shaped for showing
accounting and oversight: two public service providers, ten consuming
agencies, offerings grouped by service layer (IaaS, PaaS, SaaS, HPC & AI),
GPU components per accelerator model, per-hour plans, six quarters of
usage and invoices, users with identity attributes, offering users in
screening states, and a read-only governance user with the global support
role.

Months are written relative to the day the generator runs, and the preset
sets ``_metadata.rebase_billing_history`` so the loader moves them onto the
month it is loaded in -- a quarterly report is never empty.

Run::

    python scripts/generate_public_sector_preset.py \\
        --output src/waldur_mastermind/marketplace/demo_presets/presets/public_sector_accounting.json

Then load with::

    waldur demo_presets load public_sector_accounting -y
"""

from __future__ import annotations

import argparse
import calendar
import json
import random
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

HISTORY_MONTHS = 18  # six quarters, the newest one in progress
CURRENT_MONTH_FRACTION = 0.55  # the load month is part-way through
TAX_PERCENT = Decimal("24")

# Preset UUIDs are 32 hex characters. Every entity starts with "5e" and a
# two-character kind code, so the preset's rows are easy to spot in a DB.
KINDS = {
    "user": "01",
    "user_agreement": "02",
    "customer": "0c",
    "service_provider": "0d",
    "project": "0e",
    "category_group": "0f",
    "category": "10",
    "offering": "11",
    "offering_component": "12",
    "plan": "13",
    "resource": "14",
    "user_role": "15",
    "offering_user": "16",
    "component_usage": "17",
    "component_user_usage": "18",
    "invoice": "19",
    "invoice_item": "1a",
    "checklist": "1b",
    "question": "1c",
    "question_option": "1d",
    "maintenance": "1e",
    "maintenance_offering": "1f",
    "cost_policy": "20",
}


def make_uuid(kind: str, n: int) -> str:
    return f"5e{KINDS[kind]}{n:028x}"


def money(value) -> str:
    return str(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    index = year * 12 + month - 1 + offset
    return index // 12, index % 12 + 1


# --------------------------------------------------------------------------
# Catalogue: who provides what, in which service layer, at which price.


@dataclass
class Component:
    key: str
    type: str
    name: str
    unit: str
    billing_type: str  # usage | fixed | limit
    price: str
    article_code: str
    description: str = ""
    limit_period: str | None = None


@dataclass
class OfferingSpec:
    key: str
    name: str
    provider: str
    category: str
    type: str
    plan_unit: str
    components: list[Component]
    description: str
    plans: list[tuple[str, str, dict]] = field(default_factory=list)
    compliance: bool = False
    per_user_usage: bool = False
    plugin_options: dict = field(default_factory=dict)


SERVICE_LAYERS = [
    (
        "iaas",
        "IaaS - Infrastructure",
        "Virtual machines, storage and networks run by the State Cloud Services Centre.",
        [
            ("vm", "Virtual Machines", "General-purpose and memory-optimised VMs"),
            ("storage", "Storage", "Block and S3-compatible object storage"),
        ],
    ),
    (
        "paas",
        "PaaS - Platforms",
        "Managed platforms billed by the hour they run.",
        [
            ("k8s", "Managed Kubernetes", "Hardened Kubernetes clusters"),
            ("db", "Managed Databases", "PostgreSQL with backups and failover"),
        ],
    ),
    (
        "saas",
        "SaaS - Applications",
        "Ready-to-use applications licensed per seat.",
        [("collab", "Collaboration", "Document collaboration for public servants")],
    ),
    (
        "hpc",
        "HPC & AI",
        "Batch HPC and GPU compute from the National HPC & AI Centre.",
        [
            ("hpc", "HPC Compute", "CPU batch compute on the national cluster"),
            ("gpu", "GPU & AI Compute", "Accelerated compute, metered per GPU model"),
        ],
    ),
]

OFFERINGS = [
    OfferingSpec(
        key="vm",
        name="Government VM Cloud",
        provider="scsc",
        category="vm",
        type="Marketplace.Basic",
        plan_unit="month",
        description="Sovereign IaaS virtual machines hosted in two state data centres. "
        "Metered hourly by vCPU and RAM, invoiced monthly.",
        components=[
            Component(
                "vm",
                "vcpu_hours",
                "vCPU",
                "vCPU-hours",
                "usage",
                "0.021",
                "IAAS-VCPU-H",
            ),
            Component(
                "vm", "ram_gb_hours", "RAM", "GB-hours", "usage", "0.0045", "IAAS-RAM-H"
            ),
        ],
        plans=[("Pay as you go", "", {})],
    ),
    OfferingSpec(
        key="storage",
        name="Sovereign Object Storage",
        provider="scsc",
        category="storage",
        type="Marketplace.Basic",
        plan_unit="month",
        description="S3-compatible object storage with data kept in-country.",
        components=[
            Component(
                "storage",
                "storage_tb_months",
                "Object storage",
                "TB-months",
                "usage",
                "18.00",
                "IAAS-OBJ-TB",
            ),
        ],
        plans=[("Standard", "", {})],
    ),
    OfferingSpec(
        key="k8s",
        name="Managed Kubernetes",
        provider="scsc",
        category="k8s",
        type="Marketplace.Basic",
        plan_unit="hour",
        description="CIS-hardened Kubernetes. The control plane is billed per hour it runs; "
        "worker capacity is metered in vCPU-hours.",
        components=[
            Component(
                "k8s",
                "control_plane",
                "Control plane",
                "cluster-hours",
                "fixed",
                "0.14",
                "PAAS-K8S-CP",
            ),
            Component(
                "k8s",
                "worker_vcpu_hours",
                "Worker vCPU",
                "vCPU-hours",
                "usage",
                "0.019",
                "PAAS-K8S-VCPU",
            ),
        ],
        plans=[("Production cluster", "", {})],
    ),
    OfferingSpec(
        key="db",
        name="Managed PostgreSQL",
        provider="scsc",
        category="db",
        type="Marketplace.Basic",
        plan_unit="hour",
        description="Managed PostgreSQL with point-in-time recovery. Billed per instance-hour.",
        components=[
            Component(
                "db",
                "instance",
                "Database instance",
                "instance-hours",
                "fixed",
                "0.09",
                "PAAS-PG",
            ),
        ],
        plans=[
            ("Small (2 vCPU / 8 GB)", "0.09", {}),
            ("Large (8 vCPU / 32 GB)", "0.34", {}),
        ],
    ),
    OfferingSpec(
        key="collab",
        name="Secure Document Collaboration",
        provider="scsc",
        category="collab",
        type="Marketplace.Basic",
        plan_unit="month",
        description="Document editing and sharing for classified-up-to-restricted material, "
        "licensed per seat per month.",
        components=[
            Component(
                "collab",
                "seats",
                "Seats",
                "seats",
                "limit",
                "6.50",
                "SAAS-COLLAB",
                limit_period="month",
            ),
        ],
        plans=[("Per seat", "", {})],
    ),
    OfferingSpec(
        key="hpc",
        name="National HPC Cluster",
        provider="nhac",
        category="hpc",
        type="Marketplace.Slurm",
        plan_unit="month",
        description="CPU partition of the national cluster. Usage reported every 30 minutes "
        "by waldur-site-agent from SLURM accounting, per account and per user.",
        components=[
            Component(
                "hpc",
                "cpu_core_hours",
                "CPU",
                "core-hours",
                "usage",
                "0.012",
                "HPC-CPU-H",
            ),
            Component(
                "hpc",
                "project_storage_tb",
                "Project storage",
                "TB-months",
                "usage",
                "22.00",
                "HPC-STOR-TB",
            ),
        ],
        plans=[("Research allocation", "", {})],
        per_user_usage=True,
    ),
    OfferingSpec(
        key="gpu_h100",
        name="AI Compute - NVIDIA H100",
        provider="nhac",
        category="gpu",
        type="Marketplace.Slurm",
        plan_unit="month",
        description="NVIDIA H100 SXM 80 GB nodes for model training. Metered in GPU-hours "
        "per accelerator model. Access requires the AI compute eligibility checklist.",
        components=[
            Component(
                "gpu_h100",
                "gpu_h100_hours",
                "NVIDIA H100",
                "GPU-hours",
                "usage",
                "2.80",
                "AI-H100-H",
            ),
        ],
        plans=[("On demand", "", {})],
        compliance=True,
        per_user_usage=True,
        plugin_options={
            "gpu_model": "H100",
            "service_provider_can_create_offering_user": True,
        },
    ),
    OfferingSpec(
        key="gpu_a100",
        name="AI Compute - NVIDIA A100",
        provider="nhac",
        category="gpu",
        type="Marketplace.Slurm",
        plan_unit="month",
        description="NVIDIA A100 80 GB nodes for training and fine-tuning. Metered in GPU-hours.",
        components=[
            Component(
                "gpu_a100",
                "gpu_a100_hours",
                "NVIDIA A100",
                "GPU-hours",
                "usage",
                "1.90",
                "AI-A100-H",
            ),
        ],
        plans=[("On demand", "", {})],
        per_user_usage=True,
        plugin_options={"gpu_model": "A100"},
    ),
    OfferingSpec(
        key="gpu_l40s",
        name="AI Inference - NVIDIA L40S",
        provider="nhac",
        category="gpu",
        type="Marketplace.Slurm",
        plan_unit="month",
        description="NVIDIA L40S nodes for inference services. Metered in GPU-hours.",
        components=[
            Component(
                "gpu_l40s",
                "gpu_l40s_hours",
                "NVIDIA L40S",
                "GPU-hours",
                "usage",
                "0.95",
                "AI-L40S-H",
            ),
        ],
        plans=[("On demand", "", {})],
        plugin_options={"gpu_model": "L40S"},
    ),
    OfferingSpec(
        key="gpu_mi250x",
        name="Partner GPU - AMD MI250X (resold)",
        provider="nhac",
        category="gpu",
        type="Marketplace.Basic",
        plan_unit="month",
        description="AMD MI250X capacity bought from a European partner supercomputer and "
        "resold to national agencies. End users are registered and screened by the "
        "National HPC & AI Centre before their accounts are requested upstream.",
        components=[
            Component(
                "gpu_mi250x",
                "gpu_mi250x_hours",
                "AMD MI250X",
                "GPU-hours",
                "usage",
                "1.40",
                "AI-MI250X-H",
            ),
        ],
        plans=[("Partner allocation", "", {})],
        compliance=True,
        plugin_options={"gpu_model": "MI250X", "resold_from": "partner supercomputer"},
    ),
]

# Base monthly usage per component for a typical resource, before the
# resource's own scale and the platform's growth are applied.
BASE_USAGE = {
    "vcpu_hours": 5800,
    "ram_gb_hours": 23000,
    "storage_tb_months": 14,
    "worker_vcpu_hours": 9500,
    "cpu_core_hours": 180000,
    "project_storage_tb": 40,
    "gpu_h100_hours": 1400,
    "gpu_a100_hours": 1900,
    "gpu_l40s_hours": 2300,
    "gpu_mi250x_hours": 1100,
}

# --------------------------------------------------------------------------
# Organisations

PROVIDERS = {
    "scsc": {
        "name": "State Cloud Services Centre",
        "abbreviation": "SCSC",
        "country": "EE",
        "description": "Operates the government's sovereign cloud: IaaS, managed platforms "
        "and SaaS for public bodies.",
        "email": "service@scsc.example.gov",
        "lat": "59.4370",
        "lon": "24.7536",
    },
    "nhac": {
        "name": "National HPC & AI Centre",
        "abbreviation": "NHAC",
        "country": "EE",
        "description": "Runs the national supercomputer and AI factory, and resells partner "
        "GPU capacity to public bodies.",
        "email": "allocations@nhac.example.gov",
        "lat": "58.3780",
        "lon": "26.7290",
    },
}

# (key, name, abbreviation, country, org type, lat, lon, scale, first month index)
AGENCIES = [
    (
        "mof",
        "Ministry of Finance",
        "MOF",
        "EE",
        "government",
        "59.4353",
        "24.7430",
        1.4,
        0,
    ),
    (
        "moh",
        "Ministry of Health",
        "MOH",
        "EE",
        "government",
        "59.4390",
        "24.7480",
        1.1,
        0,
    ),
    (
        "nso",
        "National Statistics Office",
        "NSO",
        "EE",
        "government",
        "59.4260",
        "24.7690",
        1.6,
        1,
    ),
    (
        "tcb",
        "Tax and Customs Board",
        "TCB",
        "EE",
        "government",
        "59.4210",
        "24.7950",
        1.8,
        2,
    ),
    (
        "env",
        "Environmental Agency",
        "ENV",
        "EE",
        "government",
        "59.4000",
        "24.6800",
        0.9,
        4,
    ),
    (
        "city",
        "City of Riverside",
        "CITY",
        "EE",
        "municipality",
        "58.3800",
        "26.7200",
        0.6,
        6,
    ),
    (
        "uni",
        "National University of Technology",
        "NUT",
        "EE",
        "university",
        "59.3950",
        "24.6710",
        2.2,
        3,
    ),
    (
        "phri",
        "Public Health Research Institute",
        "PHRI",
        "EE",
        "research",
        "58.3700",
        "26.7100",
        1.3,
        7,
    ),
    (
        "land",
        "Land and Spatial Board",
        "LSB",
        "LV",
        "government",
        "56.9496",
        "24.1052",
        0.8,
        9,
    ),
    (
        "meteo",
        "Nordic Meteorological Service",
        "NMS",
        "FI",
        "government",
        "60.1699",
        "24.9384",
        1.5,
        11,
    ),
]

ORG_TYPE_URN = {
    "government": "urn:schac:homeOrganizationType:int:other",
    "municipality": "urn:schac:homeOrganizationType:int:other",
    "university": "urn:schac:homeOrganizationType:int:university",
    "research": "urn:schac:homeOrganizationType:int:research-institution",
}

# (agency, project name, OECD FOS code, offering keys)
PROJECTS = [
    ("mof", "Budget Forecasting Platform", "5.2", ["vm", "db", "k8s"]),
    ("mof", "e-Invoicing Gateway", "5.2", ["k8s", "db", "collab"]),
    ("moh", "Patient Registry Modernisation", "3.3", ["vm", "db", "storage"]),
    ("moh", "Clinical Imaging AI", "3.2", ["gpu_a100", "storage"]),
    ("nso", "Census 2026 Processing", "5.4", ["hpc", "storage", "vm"]),
    ("nso", "Statistical Disclosure Control", "1.1", ["hpc", "collab"]),
    ("tcb", "Customs Risk Analytics", "5.2", ["gpu_h100", "k8s", "db"]),
    ("tcb", "Tax Return Processing", "5.2", ["vm", "db", "collab"]),
    ("env", "Air Quality Monitoring", "1.5", ["vm", "storage"]),
    ("env", "Flood Risk Modelling", "1.5", ["hpc", "gpu_mi250x"]),
    ("city", "Smart Mobility Pilot", "2.1", ["k8s", "collab"]),
    ("uni", "Materials Simulation", "1.3", ["hpc", "gpu_a100"]),
    ("uni", "Estonian Language Model", "6.2", ["gpu_h100", "gpu_l40s", "storage"]),
    ("uni", "Teaching Cluster", "1.2", ["k8s", "vm"]),
    ("phri", "Genomic Surveillance", "1.6", ["hpc", "gpu_mi250x", "storage"]),
    ("land", "Orthophoto Processing", "1.5", ["gpu_l40s", "storage"]),
    ("meteo", "Numerical Weather Prediction", "1.5", ["hpc", "gpu_h100"]),
    ("meteo", "Climate Reanalysis Archive", "1.5", ["storage", "vm"]),
]

# --------------------------------------------------------------------------
# People. Everyone signs in with password "demo".

# (username, first, last, email domain, job title, nationality, residence,
#  org type, identity source, registration method, assurance, is_staff, is_support)
PEOPLE = [
    (
        "staff",
        "Platform",
        "Operator",
        "scsc.example.gov",
        "Platform administrator",
        "EE",
        "EE",
        "government",
        "local",
        "local",
        "low",
        True,
        False,
    ),
    (
        "governance",
        "Grete",
        "Auditor",
        "oversight.example.gov",
        "Oversight analyst, Public Governance Body",
        "EE",
        "EE",
        "government",
        "tara",
        "tara",
        "high",
        False,
        True,
    ),
    (
        "scsc_owner",
        "Siim",
        "Saar",
        "scsc.example.gov",
        "Head of Cloud Services",
        "EE",
        "EE",
        "government",
        "tara",
        "tara",
        "high",
        False,
        False,
    ),
    (
        "nhac_owner",
        "Hanna",
        "Kask",
        "nhac.example.gov",
        "Director, National HPC & AI Centre",
        "EE",
        "EE",
        "research",
        "eduteams",
        "eduteams",
        "medium",
        False,
        False,
    ),
    (
        "nhac_reviewer",
        "Mart",
        "Tamm",
        "nhac.example.gov",
        "User compliance officer",
        "EE",
        "EE",
        "research",
        "eduteams",
        "eduteams",
        "medium",
        False,
        False,
    ),
    (
        "mof_owner",
        "Liis",
        "Mets",
        "fin.example.gov",
        "CIO",
        "EE",
        "EE",
        "government",
        "tara",
        "tara",
        "high",
        False,
        False,
    ),
    (
        "moh_owner",
        "Peeter",
        "Ilves",
        "sm.example.gov",
        "Head of Digital Health",
        "EE",
        "EE",
        "government",
        "tara",
        "tara",
        "high",
        False,
        False,
    ),
    (
        "nso_owner",
        "Kadri",
        "Lepp",
        "stat.example.gov",
        "Head of Methodology",
        "EE",
        "EE",
        "government",
        "tara",
        "tara",
        "high",
        False,
        False,
    ),
    (
        "tcb_owner",
        "Andres",
        "Rebane",
        "emta.example.gov",
        "Chief Data Officer",
        "EE",
        "EE",
        "government",
        "tara",
        "tara",
        "high",
        False,
        False,
    ),
    (
        "env_owner",
        "Maarja",
        "Kuusk",
        "envir.example.gov",
        "Head of Monitoring",
        "EE",
        "EE",
        "government",
        "tara",
        "tara",
        "high",
        False,
        False,
    ),
    (
        "city_owner",
        "Toomas",
        "Vaher",
        "riverside.example.gov",
        "Smart City Lead",
        "EE",
        "EE",
        "municipality",
        "tara",
        "tara",
        "high",
        False,
        False,
    ),
    (
        "uni_owner",
        "Katrin",
        "Pärn",
        "nut.example.edu",
        "Head of Research Computing",
        "EE",
        "EE",
        "university",
        "eduteams",
        "eduteams",
        "medium",
        False,
        False,
    ),
    (
        "phri_owner",
        "Jaan",
        "Sepp",
        "phri.example.gov",
        "Bioinformatics Lead",
        "EE",
        "EE",
        "research",
        "eduteams",
        "eduteams",
        "medium",
        False,
        False,
    ),
    (
        "land_owner",
        "Ilze",
        "Bērziņa",
        "lsb.example.gov.lv",
        "GIS Manager",
        "LV",
        "LV",
        "government",
        "eduteams",
        "eduteams",
        "medium",
        False,
        False,
    ),
    (
        "meteo_owner",
        "Aino",
        "Virtanen",
        "nms.example.fi",
        "Head of Modelling",
        "FI",
        "FI",
        "government",
        "eduteams",
        "eduteams",
        "medium",
        False,
        False,
    ),
    (
        "analyst1",
        "Karl",
        "Mägi",
        "stat.example.gov",
        "Data scientist",
        "EE",
        "EE",
        "government",
        "tara",
        "tara",
        "high",
        False,
        False,
    ),
    (
        "analyst2",
        "Olga",
        "Ivanova",
        "emta.example.gov",
        "Machine-learning engineer",
        "EE",
        "EE",
        "government",
        "tara",
        "tara",
        "substantial",
        False,
        False,
    ),
    (
        "researcher1",
        "Lukas",
        "Weber",
        "nut.example.edu",
        "Postdoctoral researcher",
        "DE",
        "EE",
        "university",
        "eduteams",
        "eduteams",
        "medium",
        False,
        False,
    ),
    (
        "researcher2",
        "Sofia",
        "Rossi",
        "nut.example.edu",
        "PhD candidate",
        "IT",
        "EE",
        "university",
        "eduteams",
        "eduteams",
        "low",
        False,
        False,
    ),
    (
        "researcher3",
        "Mikk",
        "Org",
        "phri.example.gov",
        "Epidemiologist",
        "EE",
        "EE",
        "research",
        "eduteams",
        "eduteams",
        "medium",
        False,
        False,
    ),
    (
        "contractor1",
        "Ravi",
        "Menon",
        "vendor.example.com",
        "External contractor",
        "IN",
        "EE",
        "other",
        "local",
        "local",
        "low",
        False,
        False,
    ),
    (
        "forecaster1",
        "Emil",
        "Nieminen",
        "nms.example.fi",
        "Forecaster",
        "FI",
        "FI",
        "government",
        "eduteams",
        "eduteams",
        "medium",
        False,
        False,
    ),
]

ASSURANCE = {
    "low": ["https://refeds.org/assurance/IAP/low"],
    "medium": [
        "https://refeds.org/assurance/IAP/low",
        "https://refeds.org/assurance/IAP/medium",
    ],
    "substantial": [
        "https://refeds.org/assurance/IAP/low",
        "https://refeds.org/assurance/IAP/medium",
    ],
    "high": [
        "https://refeds.org/assurance/IAP/low",
        "https://refeds.org/assurance/IAP/medium",
        "https://refeds.org/assurance/IAP/high",
    ],
}

# Project members beyond the agency owner: (username, project name, role)
MEMBERS = [
    ("analyst1", "Census 2026 Processing", "PROJECT.MEMBER"),
    ("analyst1", "Statistical Disclosure Control", "PROJECT.MEMBER"),
    ("analyst2", "Customs Risk Analytics", "PROJECT.MANAGER"),
    ("contractor1", "Customs Risk Analytics", "PROJECT.MEMBER"),
    ("researcher1", "Materials Simulation", "PROJECT.MANAGER"),
    ("researcher1", "Estonian Language Model", "PROJECT.MEMBER"),
    ("researcher2", "Estonian Language Model", "PROJECT.MEMBER"),
    ("researcher3", "Genomic Surveillance", "PROJECT.MANAGER"),
    ("forecaster1", "Numerical Weather Prediction", "PROJECT.MEMBER"),
]

# Offering accounts in states that show the screening flow:
# (username, offering key, state, restricted, provider comment)
SCREENING = [
    ("analyst2", "gpu_h100", 5, False, ""),
    ("researcher1", "gpu_h100", 5, False, ""),
    (
        "researcher2",
        "gpu_h100",
        4,
        False,
        "Assurance level below 'medium'. Please verify your identity through eduTEAMS "
        "and re-submit the eligibility checklist.",
    ),
    (
        "contractor1",
        "gpu_h100",
        4,
        False,
        "External contractor: a signed data-processing agreement from Tax and Customs "
        "Board is required before the account is activated.",
    ),
    ("forecaster1", "gpu_h100", 5, False, ""),
    (
        "uni_owner",
        "gpu_h100",
        5,
        True,
        "Restricted pending the quarterly access review.",
    ),
    ("researcher3", "gpu_mi250x", 5, False, ""),
    (
        "env_owner",
        "gpu_mi250x",
        4,
        False,
        "Account request forwarded to the partner site; awaiting their approval.",
    ),
]


# --------------------------------------------------------------------------


class Builder:
    def __init__(self, today: date, seed: int):
        self.today = today
        self.rng = random.Random(seed)
        self.counters: dict[str, int] = {}
        self.data: dict = {
            "_metadata": {
                "title": "Public Sector Accounting & Oversight",
                "description": (
                    "A sovereign public-sector cloud: two public service providers "
                    "(State Cloud Services Centre, National HPC & AI Centre) serving ten "
                    "agencies, municipalities and universities. Offerings are grouped by "
                    "service layer, GPUs are metered per accelerator model, PaaS is billed "
                    "per hour, and six quarters of usage and invoices end in the current "
                    "month. Includes offering users in screening states and a governance "
                    "user with read-only global access for oversight reporting."
                ),
                "version": "1.0.0",
                "rebase_billing_history": True,
                "scenarios": [
                    "Quarterly oversight report: usage, cost and growth per agency and quarter",
                    "Metering per service layer (IaaS / PaaS / SaaS / HPC & AI category groups)",
                    "Metering per chip type (H100, A100, L40S, AMD MI250X GPU-hours)",
                    "Per-hour billing of managed Kubernetes and PostgreSQL plans",
                    "User demographics: nationality, organisation type, identity source, assurance",
                    "Screening end users of a GPU offering with a compliance checklist",
                    "Offering users pending additional validation or restricted by the provider",
                    "Resold partner GPU capacity screened by the reselling provider",
                    "Governance-body user with the global support role, no staff rights",
                    "White-labelled portal with anonymous catalogue browsing disabled",
                ],
            }
        }
        self.months = [
            shift_month(today.year, today.month, offset)
            for offset in range(-(HISTORY_MONTHS - 1), 1)
        ]

    def next_uuid(self, kind: str) -> str:
        self.counters[kind] = self.counters.get(kind, 0) + 1
        return make_uuid(kind, self.counters[kind])

    def add(self, collection: str, row: dict) -> dict:
        self.data.setdefault(collection, []).append(row)
        return row

    # -- building blocks ---------------------------------------------------

    def build(self) -> dict:
        self.build_agreements()
        self.build_users()
        self.build_organisations()
        self.build_catalogue()
        self.build_projects_and_roles()
        self.build_resources()
        self.build_usage_and_invoices()
        self.build_screening()
        self.build_maintenance()
        self.build_policies()
        self.build_settings()
        return self.data

    def build_agreements(self):
        self.add(
            "user_agreements",
            {
                "uuid": self.next_uuid("user_agreement"),
                "agreement_type": "TOS",
                "content": (
                    "# Public Sector Cloud - Terms of Use\n\n"
                    "Services are available to public bodies and their authorised staff and "
                    "contractors. Access is personal; accounts are reviewed quarterly. Usage "
                    "and cost data are reported to the Public Governance Body every quarter."
                ),
            },
        )
        self.add(
            "user_agreements",
            {
                "uuid": self.next_uuid("user_agreement"),
                "agreement_type": "PP",
                "content": (
                    "# Privacy Notice\n\n"
                    "We process identity attributes received from the national eID (TARA) or "
                    "eduTEAMS to verify eligibility. Service providers receive only the "
                    "attributes configured for each offering."
                ),
            },
        )

    def build_users(self):
        self.users = {}
        for n, person in enumerate(PEOPLE, start=1):
            (
                username,
                first,
                last,
                domain,
                title,
                nationality,
                residence,
                org_type,
                identity_source,
                registration,
                assurance,
                is_staff,
                is_support,
            ) = person
            uuid = self.next_uuid("user")
            self.users[username] = uuid
            self.add(
                "users",
                {
                    "uuid": uuid,
                    "username": username,
                    "email": f"{username}@{domain}",
                    "first_name": first,
                    "last_name": last,
                    "is_staff": is_staff,
                    "is_support": is_support,
                    "is_active": True,
                    "password": "demo",
                    "job_title": title,
                    "organization": domain,
                    "registration_method": registration,
                    "identity_source": identity_source,
                    "nationality": nationality,
                    "nationalities": [nationality],
                    "country_of_residence": residence,
                    "organization_country": residence,
                    "organization_type": ORG_TYPE_URN.get(
                        org_type, "urn:schac:homeOrganizationType:int:other"
                    ),
                    "affiliations": [f"member@{domain}", f"employee@{domain}"],
                    "eduperson_assurance": ASSURANCE[assurance],
                    "preferred_language": "en",
                    "agreement_date": "2025-01-01T00:00:00Z",
                    "phone_number": f"+372 5{n:03d} {1000 + n}",
                },
            )

    def build_organisations(self):
        self.customers = {}
        self.service_providers = {}
        for key, spec in PROVIDERS.items():
            uuid = self.next_uuid("customer")
            self.customers[key] = uuid
            self.add(
                "customers",
                {
                    "uuid": uuid,
                    "name": spec["name"],
                    "abbreviation": spec["abbreviation"],
                    "description": spec["description"],
                    "email": spec["email"],
                    "country": spec["country"],
                    "latitude": spec["lat"],
                    "longitude": spec["lon"],
                    "accounting_start_date": "2024-01-01T00:00:00",
                    "default_tax_percent": str(TAX_PERCENT),
                },
            )
            sp_uuid = self.next_uuid("service_provider")
            self.service_providers[key] = sp_uuid
            self.add(
                "service_providers",
                {
                    "uuid": sp_uuid,
                    "customer_uuid": uuid,
                    "description": spec["description"],
                    "enable_notifications": True,
                },
            )

        self.agency_meta = {}
        for key, name, abbr, country, org_type, lat, lon, scale, first in AGENCIES:
            uuid = self.next_uuid("customer")
            self.customers[key] = uuid
            self.agency_meta[key] = {"scale": scale, "first": first, "name": name}
            year, month = self.months[first]
            self.add(
                "customers",
                {
                    "uuid": uuid,
                    "name": name,
                    "abbreviation": abbr,
                    "description": f"{name} ({org_type}).",
                    "email": f"it@{abbr.lower()}.example.gov",
                    "country": country,
                    "latitude": lat,
                    "longitude": lon,
                    "accounting_start_date": f"{year}-{month:02d}-01T00:00:00",
                    "default_tax_percent": str(TAX_PERCENT),
                },
            )

    def build_catalogue(self):
        self.categories = {}
        for layer_key, title, description, cats in SERVICE_LAYERS:
            group_uuid = self.next_uuid("category_group")
            self.add(
                "category_groups",
                {
                    "uuid": group_uuid,
                    "title": title,
                    "description": description,
                },
            )
            for cat_key, cat_title, cat_description in cats:
                uuid = self.next_uuid("category")
                self.categories[cat_key] = (uuid, title, cat_title)
                self.add(
                    "categories",
                    {
                        "uuid": uuid,
                        "title": cat_title,
                        "description": cat_description,
                        "group_uuid": group_uuid,
                    },
                )

        checklist_uuid = self.build_compliance_checklist()

        self.offerings = {}
        for spec in OFFERINGS:
            uuid = self.next_uuid("offering")
            row = {
                "uuid": uuid,
                "name": spec.name,
                "type": spec.type,
                "state": 2,
                "shared": True,
                "billable": True,
                "category_uuid": self.categories[spec.category][0],
                "customer_uuid": self.customers[spec.provider],
                "description": spec.description,
                "plugin_options": spec.plugin_options,
                "country": "EE",
            }
            if spec.compliance:
                row["compliance_checklist_uuid"] = checklist_uuid
            self.add("offerings", row)

            components = {}
            for comp in spec.components:
                comp_uuid = self.next_uuid("offering_component")
                components[comp.type] = (comp_uuid, comp)
                comp_row = {
                    "uuid": comp_uuid,
                    "offering_uuid": uuid,
                    "type": comp.type,
                    "name": comp.name,
                    "description": comp.description
                    or f"{comp.name}, billed per {comp.unit}",
                    "billing_type": comp.billing_type,
                    "measured_unit": comp.unit,
                    "article_code": comp.article_code,
                }
                if comp.limit_period:
                    comp_row["limit_period"] = comp.limit_period
                self.add("offering_components", comp_row)

            plans = []
            for plan_name, price_override, _ in spec.plans:
                plan_uuid = self.next_uuid("plan")
                self.add(
                    "plans",
                    {
                        "uuid": plan_uuid,
                        "name": plan_name,
                        "offering_uuid": uuid,
                        "unit": spec.plan_unit,
                        "description": f"{plan_name} - billed per {spec.plan_unit}",
                    },
                )
                prices = {}
                for comp_type, (comp_uuid, comp) in components.items():
                    price = (
                        price_override
                        if (price_override and comp.billing_type == "fixed")
                        else comp.price
                    )
                    prices[comp_type] = Decimal(price)
                    self.add(
                        "plan_components",
                        {
                            "plan_uuid": plan_uuid,
                            "component_uuid": comp_uuid,
                            "price": price,
                            "amount": 1 if comp.billing_type == "fixed" else 0,
                        },
                    )
                plans.append((plan_uuid, plan_name, prices))

            self.offerings[spec.key] = {
                "uuid": uuid,
                "spec": spec,
                "components": components,
                "plans": plans,
            }

    def build_compliance_checklist(self) -> str:
        uuid = self.next_uuid("checklist")
        self.add(
            "checklists",
            {
                "uuid": uuid,
                "name": "AI compute eligibility",
                "description": (
                    "Every user of national GPU capacity answers these questions before the "
                    "provider activates the account. Answers flagged for review are checked "
                    "by the NHAC user compliance officer."
                ),
                "checklist_type": "offering_compliance",
            },
        )
        questions = [
            (
                "Are you employed by, or contracted to, an eligible public body?",
                "boolean",
                True,
                None,
            ),
            (
                "Will you process personal or classified data on this service?",
                "boolean",
                True,
                True,
            ),
            (
                "Is the work subject to export-control or dual-use restrictions?",
                "boolean",
                True,
                True,
            ),
            ("Country of your employing organisation", "country", True, None),
            ("Describe the intended use of the GPU capacity", "text_area", True, None),
        ]
        for order, (text, qtype, required, review_value) in enumerate(
            questions, start=1
        ):
            row = {
                "uuid": self.next_uuid("question"),
                "checklist_uuid": uuid,
                "description": text,
                "question_type": qtype,
                "required": required,
                "order": order,
            }
            if review_value is not None:
                row["review_answer_value"] = review_value
                row["operator"] = "equals"
            self.add("questions", row)
        return uuid

    def build_projects_and_roles(self):
        self.projects = {}
        for agency, name, oecd, offering_keys in PROJECTS:
            uuid = self.next_uuid("project")
            self.projects[name] = (uuid, agency, offering_keys)
            self.add(
                "projects",
                {
                    "uuid": uuid,
                    "name": name,
                    "description": f"{name} - {self.agency_meta[agency]['name']}",
                    "customer_uuid": self.customers[agency],
                    "oecd_fos_2007_code": oecd,
                },
            )

        def role(username, role_name, scope_type, scope_uuid):
            self.add(
                "user_roles",
                {
                    "uuid": self.next_uuid("user_role"),
                    "user_uuid": self.users[username],
                    "user_username": username,
                    "role_name": role_name,
                    "scope_type": scope_type,
                    "scope_uuid": scope_uuid,
                    "is_active": True,
                },
            )

        role(
            "scsc_owner", "CUSTOMER.OWNER", "structure.customer", self.customers["scsc"]
        )
        role(
            "nhac_owner", "CUSTOMER.OWNER", "structure.customer", self.customers["nhac"]
        )
        role(
            "nhac_reviewer",
            "CUSTOMER.MANAGER",
            "structure.customer",
            self.customers["nhac"],
        )
        for key, *_ in AGENCIES:
            role(
                f"{key}_owner",
                "CUSTOMER.OWNER",
                "structure.customer",
                self.customers[key],
            )
        for username, project, role_name in MEMBERS:
            role(username, role_name, "structure.project", self.projects[project][0])

    def build_resources(self):
        self.resources = []
        for project_name, (
            project_uuid,
            agency,
            offering_keys,
        ) in self.projects.items():
            meta = self.agency_meta[agency]
            for index, key in enumerate(offering_keys):
                offering = self.offerings[key]
                plan_uuid, plan_name, prices = self.rng.choice(offering["plans"])
                start = min(
                    meta["first"] + index * self.rng.randint(1, 3), HISTORY_MONTHS - 2
                )
                slug = key.replace("_", "-")
                name = f"{project_name.lower().replace(' ', '-')[:24]}-{slug}"
                limits = {}
                for comp_type, (_, comp) in offering["components"].items():
                    if comp.billing_type == "limit":
                        limits[comp_type] = int(40 * meta["scale"]) + self.rng.randint(
                            0, 20
                        )
                year, month = self.months[start]
                uuid = self.next_uuid("resource")
                self.add(
                    "resources",
                    {
                        "uuid": uuid,
                        "name": name,
                        "offering_uuid": offering["uuid"],
                        "project_uuid": project_uuid,
                        "plan_uuid": plan_uuid,
                        "state": 2,
                        "limits": limits,
                        "attributes": {"name": name},
                        "created": f"{year}-{month:02d}-0{self.rng.randint(1, 9)}T09:00:00",
                    },
                )
                self.resources.append(
                    {
                        "uuid": uuid,
                        "name": name,
                        "project_uuid": project_uuid,
                        "agency": agency,
                        "offering": offering,
                        "plan_name": plan_name,
                        "prices": prices,
                        "limits": limits,
                        "start": start,
                        "scale": meta["scale"] * self.rng.uniform(0.6, 1.4),
                    }
                )

    def project_users(self, project_uuid: str) -> list[str]:
        names = [u for u, p, _ in MEMBERS if self.projects[p][0] == project_uuid]
        agency = next(
            a for (uuid, a, _) in self.projects.values() if uuid == project_uuid
        )
        return [f"{agency}_owner", *names]

    def build_usage_and_invoices(self):
        invoices: dict[tuple[str, int], dict] = {}
        last = HISTORY_MONTHS - 1

        for res in self.resources:
            offering = res["offering"]
            spec: OfferingSpec = offering["spec"]
            provider_name = PROVIDERS[spec.provider]["name"]
            for m in range(res["start"], HISTORY_MONTHS):
                year, month = self.months[m]
                days = calendar.monthrange(year, month)[1]
                fraction = CURRENT_MONTH_FRACTION if m == last else 1.0
                # Platform adoption grows ~3% a month, with a summer dip and a
                # year-end peak, so quarter-on-quarter figures differ.
                growth = 1.03 ** (m - res["start"])
                season = {7: 0.8, 8: 0.85, 12: 1.15, 1: 0.95}.get(month, 1.0)
                end_day = max(1, round(days * fraction))
                period_start = f"{year}-{month:02d}-01T00:00:00"
                period_end = f"{year}-{month:02d}-{end_day:02d}T23:59:59"

                items = []
                for comp_type, (comp_uuid, comp) in offering["components"].items():
                    price = res["prices"][comp_type]
                    if comp.billing_type == "usage":
                        base = BASE_USAGE[comp_type]
                        noise = self.rng.uniform(0.85, 1.15)
                        amount = Decimal(
                            str(
                                round(
                                    base
                                    * res["scale"]
                                    * growth
                                    * season
                                    * noise
                                    * fraction,
                                    2,
                                )
                            )
                        )
                        if comp_type.endswith("_tb_months") or comp_type.endswith(
                            "_tb"
                        ):
                            amount = amount.quantize(Decimal("0.01"))
                        else:
                            amount = amount.quantize(Decimal("1"))
                        usage_uuid = self.next_uuid("component_usage")
                        self.add(
                            "component_usages",
                            {
                                "uuid": usage_uuid,
                                "resource_uuid": res["uuid"],
                                "component_uuid": comp_uuid,
                                "usage": str(amount),
                                "date": f"{year}-{month:02d}-{end_day:02d}T23:00:00",
                                "billing_period": f"{year}-{month:02d}-01",
                                "description": f"{comp.name} usage for {year}-{month:02d}",
                            },
                        )
                        if spec.per_user_usage and comp_type != "project_storage_tb":
                            self.split_user_usage(usage_uuid, res, amount, comp)
                        quantity = amount
                    elif comp.billing_type == "fixed":
                        # Plan unit is "hour": a running cluster or database is
                        # billed for every hour of the period.
                        quantity = Decimal(end_day * 24)
                    else:  # limit - seats per month
                        quantity = Decimal(res["limits"][comp_type])
                        if fraction < 1:
                            quantity = (quantity * Decimal(str(fraction))).quantize(
                                Decimal("0.01")
                            )
                    items.append((comp, comp_type, quantity, price))

                if m == last:
                    # The loader bills the current month for fixed and limit
                    # components itself; only usage is pre-seeded for it.
                    items = [i for i in items if i[0].billing_type == "usage"]

                key = (res["agency"], m)
                invoice = invoices.get(key)
                if invoice is None:
                    invoice = {
                        "uuid": self.next_uuid("invoice"),
                        "customer_uuid": self.customers[res["agency"]],
                        "year": year,
                        "month": month,
                        "state": "pending" if m == last else "paid",
                        "tax_percent": str(TAX_PERCENT),
                        "created": f"{year}-{month:02d}-01",
                        "total": Decimal(0),
                    }
                    if m != last:
                        next_year, next_month = shift_month(year, month, 1)
                        invoice["invoice_date"] = f"{next_year}-{next_month:02d}-05"
                    invoices[key] = invoice

                for comp, comp_type, quantity, price in items:
                    cost = quantity * price
                    invoice["total"] += cost
                    self.add(
                        "invoice_items",
                        {
                            "uuid": self.next_uuid("invoice_item"),
                            "invoice_uuid": invoice["uuid"],
                            "resource_uuid": res["uuid"],
                            "project_uuid": res["project_uuid"],
                            "name": f"{res['name']} ({spec.name} / {comp.name})",
                            "quantity": str(quantity),
                            "measured_unit": comp.unit,
                            "unit": (
                                spec.plan_unit
                                if comp.billing_type == "fixed"
                                else "month"
                                if comp.billing_type == "limit"
                                else "quantity"
                            ),
                            "unit_price": str(price),
                            "article_code": comp.article_code,
                            "start": period_start,
                            "end": period_end,
                            "details": {
                                "offering_name": spec.name,
                                "offering_uuid": offering["uuid"],
                                "offering_type": spec.type,
                                "offering_component_type": comp_type,
                                "offering_component_name": comp.name,
                                "service_provider_name": provider_name,
                                "service_category_title": self.categories[
                                    spec.category
                                ][2],
                                "service_layer": self.categories[spec.category][1],
                                "plan_name": res["plan_name"],
                                "resource_name": res["name"],
                                "unit": spec.plan_unit
                                if comp.billing_type == "fixed"
                                else "quantity",
                            },
                        },
                    )

        for invoice in sorted(invoices.values(), key=lambda i: (i["year"], i["month"])):
            total = invoice.pop("total")
            invoice["total_cost"] = money(total)
            invoice["total_price"] = money(total * (1 + TAX_PERCENT / 100))
            self.add("invoices", invoice)

    def split_user_usage(self, usage_uuid, res, amount: Decimal, comp: Component):
        users = self.project_users(res["project_uuid"])
        weights = [self.rng.uniform(0.3, 1.0) for _ in users]
        total_weight = sum(weights)
        remaining = amount
        for i, (username, weight) in enumerate(zip(users, weights)):
            share = (
                remaining
                if i == len(users) - 1
                else (amount * Decimal(str(weight / total_weight))).quantize(
                    Decimal("1")
                )
            )
            remaining -= share
            self.add(
                "component_user_usages",
                {
                    "uuid": self.next_uuid("component_user_usage"),
                    "component_usage_uuid": usage_uuid,
                    "user_uuid": self.users[username],
                    "username": username,
                    "usage": str(share),
                    "description": f"{username}'s {comp.name} usage",
                },
            )

    def build_screening(self):
        for username, offering_key, state, restricted, comment in SCREENING:
            row = {
                "uuid": self.next_uuid("offering_user"),
                "offering_uuid": self.offerings[offering_key]["uuid"],
                "user_uuid": self.users[username],
                # An account still awaiting validation has no backend username
                # yet; creating one with a username would mark it OK at once.
                "username": "" if state == 4 else f"{username.replace('_', '')}01",
                "state": state,
                "is_restricted": restricted,
            }
            if comment:
                row["service_provider_comment"] = comment
                row["service_provider_comment_url"] = (
                    "https://nhac.example.gov/eligibility"
                )
            self.add("offering_users", row)

    def build_maintenance(self):
        # Maintenance windows are not rebased with billing history, so they
        # are written relative to the generation date only.
        y, m = shift_month(self.today.year, self.today.month, -1)
        windows = [
            (
                "HPC cluster firmware upgrade",
                "nhac",
                ["hpc", "gpu_h100", "gpu_a100"],
                4,
                4,
                (y, m, 9, 6, 14),
                (6, 20, 15, 10),
                3,
                "Compute nodes unavailable while firmware and drivers are upgraded.",
            ),
            (
                "Object storage network maintenance",
                "scsc",
                ["storage"],
                1,
                4,
                (y, m, 21, 22, 23),
                (22, 5, 23, 40),
                2,
                "Short interruptions to object storage writes.",
            ),
        ]
        for (
            name,
            provider,
            offering_keys,
            mtype,
            state,
            sched,
            actual,
            impact,
            message,
        ) in windows:
            year, month, day, start_h, end_h = sched
            actual_start_h, actual_start_m, actual_end_h, actual_end_m = actual
            uuid = self.next_uuid("maintenance")
            self.add(
                "maintenance_announcements",
                {
                    "uuid": uuid,
                    "name": name,
                    "message": message,
                    "maintenance_type": mtype,
                    "state": state,
                    "scheduled_start": f"{year}-{month:02d}-{day:02d}T{start_h:02d}:00:00+00:00",
                    "scheduled_end": f"{year}-{month:02d}-{day:02d}T{end_h:02d}:59:00+00:00",
                    "actual_start": f"{year}-{month:02d}-{day:02d}T{actual_start_h:02d}:{actual_start_m:02d}:00+00:00",
                    "actual_end": f"{year}-{month:02d}-{day:02d}T{actual_end_h:02d}:{actual_end_m:02d}:00+00:00",
                    "service_provider_uuid": self.service_providers[provider],
                },
            )
            for key in offering_keys:
                self.add(
                    "maintenance_announcement_offerings",
                    {
                        "uuid": self.next_uuid("maintenance_offering"),
                        "maintenance_uuid": uuid,
                        "offering_uuid": self.offerings[key]["uuid"],
                        "impact_level": impact,
                        "impact_description": message,
                    },
                )

    def build_policies(self):
        for agency, limit in (("tcb", 40000), ("uni", 60000)):
            self.add(
                "customer_estimated_cost_policies",
                {
                    "uuid": self.next_uuid("cost_policy"),
                    "customer_uuid": self.customers[agency],
                    "limit_cost": limit,
                    "period": 2,
                    "actions": "notify_organization_owners",
                    "options": {},
                    "has_fired": False,
                },
            )

    def build_settings(self):
        self.data["constance_settings"] = {
            "SITE_NAME": "Public Sector Cloud",
            "SHORT_PAGE_TITLE": "Public Sector Cloud",
            "FULL_PAGE_TITLE": "Public Sector Cloud - services for government",
            "SITE_DESCRIPTION": (
                "Sovereign cloud, HPC and AI services for public bodies, with usage "
                "reported quarterly to the Public Governance Body."
            ),
            "SITE_EMAIL": "service@scsc.example.gov",
            "SITE_ADDRESS": "1 Government Square, Tallinn",
            "CURRENCY_NAME": "EUR",
            "BRAND_COLOR": "#1F4E79",
            "SIDEBAR_STYLE": "dark",
            "LOGIN_PAGE_LAYOUT": "centered-card",
            "ANONYMOUS_USER_CAN_VIEW_OFFERINGS": False,
            "ANONYMOUS_USER_CAN_VIEW_PLANS": False,
            "USER_DATA_ACCESS_LOGGING_ENABLED": True,
            # Demographic reports only chart attributes the profile shows.
            "ENABLED_USER_PROFILE_ATTRIBUTES": [
                "phone_number",
                "organization",
                "job_title",
                "affiliations",
                "nationality",
                "country_of_residence",
                "organization_type",
                "eduperson_assurance",
            ],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="src/waldur_mastermind/marketplace/demo_presets/presets/public_sector_accounting.json",
    )
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument(
        "--today",
        help="Anchor month as YYYY-MM-DD (defaults to today); the loader rebases anyway.",
    )
    args = parser.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    data = Builder(today, args.seed).build()
    out = Path(args.output)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    counts = {k: len(v) for k, v in data.items() if isinstance(v, list)}
    print(f"Wrote {out}: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
