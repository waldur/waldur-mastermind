from django.db import transaction

SCIENCE_DOMAIN_PRESETS = {
    "cscs": {
        "label": "CSCS Science Domains",
        "description": "7 domains, 24 sub-domains (Swiss National Supercomputing Centre)",
        "domains": {
            "1": (
                "Physics",
                [
                    ("1.1", "Astrophysics & Cosmology"),
                    ("1.2", "Plasma Physics"),
                    ("1.3", "Others"),
                ],
            ),
            "2": (
                "Chemistry & Materials",
                [
                    ("2.1", "Chemical Sciences"),
                    ("2.2", "Nanoscience"),
                    ("2.3", "Materials Science"),
                    ("2.4", "Condensed Matter Physics"),
                    ("2.5", "Others"),
                ],
            ),
            "3": (
                "Earth & Environmental Science",
                [
                    ("3.1", "Geoscience"),
                    ("3.2", "Climate"),
                    ("3.3", "Others"),
                ],
            ),
            "4": (
                "Life Science",
                [
                    ("4.1", "Biological Sciences"),
                    ("4.2", "Molecular Biophysics"),
                    ("4.3", "Biomedical Engineering"),
                    ("4.4", "Bioinformatics"),
                    ("4.5", "Neuroscience"),
                    ("4.6", "Others"),
                ],
            ),
            "5": (
                "Mechanics & Engineering",
                [
                    ("5.1", "Fluid Dynamics"),
                    ("5.2", "Mechanical Engineering"),
                    ("5.3", "Others"),
                ],
            ),
            "6": (
                "Computer Science",
                [
                    ("6.1", "Applied Mathematics"),
                    ("6.2", "Computer Science"),
                    ("6.3", "Others"),
                ],
            ),
            "7": (
                "Others",
                [
                    ("7.1", "Others"),
                ],
            ),
        },
    },
    "oecd_fos_2007": {
        "label": "OECD Fields of Science 2007",
        "description": "6 domains, 43 sub-domains (OECD Fields of Science and Technology classification)",
        "domains": {
            "1": (
                "Natural Sciences",
                [
                    ("1.1", "Mathematics"),
                    ("1.2", "Computer and information sciences"),
                    ("1.3", "Physical sciences"),
                    ("1.4", "Chemical sciences"),
                    ("1.5", "Earth and related environmental sciences"),
                    ("1.6", "Biological sciences"),
                    ("1.7", "Other natural sciences"),
                ],
            ),
            "2": (
                "Engineering and Technology",
                [
                    ("2.1", "Civil engineering"),
                    (
                        "2.2",
                        "Electrical engineering, electronic engineering, information engineering",
                    ),
                    ("2.3", "Mechanical engineering"),
                    ("2.4", "Chemical engineering"),
                    ("2.5", "Materials engineering"),
                    ("2.6", "Medical engineering"),
                    ("2.7", "Environmental engineering"),
                    ("2.8", "Systems engineering"),
                    ("2.9", "Environmental biotechnology"),
                    ("2.10", "Industrial biotechnology"),
                    ("2.11", "Nano technology"),
                    ("2.12", "Other engineering and technologies"),
                ],
            ),
            "3": (
                "Medical and Health Sciences",
                [
                    ("3.1", "Basic medicine"),
                    ("3.2", "Clinical medicine"),
                    ("3.3", "Health sciences"),
                    ("3.4", "Health biotechnology"),
                    ("3.5", "Other medical sciences"),
                ],
            ),
            "4": (
                "Agricultural and Veterinary Sciences",
                [
                    ("4.1", "Agriculture, forestry, and fisheries"),
                    ("4.2", "Animal and dairy science"),
                    ("4.3", "Veterinary science"),
                    ("4.4", "Agricultural biotechnology"),
                    ("4.5", "Other agricultural sciences"),
                ],
            ),
            "5": (
                "Social Sciences",
                [
                    ("5.1", "Psychology"),
                    ("5.2", "Economics and business"),
                    ("5.3", "Educational sciences"),
                    ("5.4", "Sociology"),
                    ("5.5", "Law"),
                    ("5.6", "Political science"),
                    ("5.7", "Social and economic geography"),
                    ("5.8", "Media and communications"),
                    ("5.9", "Other social sciences"),
                ],
            ),
            "6": (
                "Humanities and the Arts",
                [
                    ("6.1", "History and archaeology"),
                    ("6.2", "Languages and literature"),
                    ("6.3", "Philosophy, ethics and religion"),
                    ("6.4", "Arts (arts, history of arts, performing arts, music)"),
                    ("6.5", "Other humanities"),
                ],
            ),
        },
    },
}


def load_preset(preset_name):
    from waldur_core.structure.models import ScienceDomain, ScienceSubDomain

    preset = SCIENCE_DOMAIN_PRESETS.get(preset_name)
    if not preset:
        raise ValueError(f"Unknown preset: {preset_name}")

    created_domains = 0
    created_subdomains = 0
    skipped_domains = 0
    skipped_subdomains = 0

    with transaction.atomic():
        for domain_code, (domain_name, subdomains) in preset["domains"].items():
            domain, created = ScienceDomain.objects.get_or_create(
                code=domain_code,
                defaults={"name": domain_name},
            )
            if created:
                created_domains += 1
            else:
                skipped_domains += 1

            for sub_code, sub_name in subdomains:
                _, sub_created = ScienceSubDomain.objects.get_or_create(
                    code=sub_code,
                    defaults={"name": sub_name, "domain": domain},
                )
                if sub_created:
                    created_subdomains += 1
                else:
                    skipped_subdomains += 1

    return {
        "created_domains": created_domains,
        "created_subdomains": created_subdomains,
        "skipped_domains": skipped_domains,
        "skipped_subdomains": skipped_subdomains,
    }
