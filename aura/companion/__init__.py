"""Aura Companion — mobile web control plane for a running Aura desktop instance."""
from aura.companion.auth import generate_ticket, get_device_display_name, get_device_id, pop_ticket
from aura.companion.manager import CompanionManager
from aura.companion.protocol import (
    ActiveRunSummary,
    CompanionProject,
    CompanionThread,
    ReceiptSummary,
    make_envelope,
    parse_command,
)
