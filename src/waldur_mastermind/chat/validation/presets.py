"""Whether a scenario pack's demo data is actually in the database.

Packs that assert concrete values (project names, credit figures) are only
meaningful against the preset they were generated from. Running them
without it scores the assistant on data it was never given, so the harness
skips them instead.
"""

import json
import logging

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace.demo_presets.manifest import DemoPresetManager

logger = logging.getLogger(__name__)


def is_preset_loaded(name: str) -> bool:
    """True when every customer the preset defines exists in the database.

    Customers are the preset's roots — projects, resources and credits all
    hang off them — so their presence is a cheap proxy for "this preset was
    loaded". A partial load counts as absent: the packs assert on figures
    that a half-loaded preset would get wrong.
    """
    path = DemoPresetManager.get_preset_path(name)
    if path is None:
        return False
    try:
        customers = json.loads(path.read_text()).get("customers") or []
    except (OSError, ValueError):
        logger.exception("Failed to read demo preset %s", name)
        return False
    uuids = [customer["uuid"] for customer in customers if customer.get("uuid")]
    if not uuids:
        return False
    present = structure_models.Customer.objects.filter(uuid__in=uuids).count()
    return present == len(uuids)
