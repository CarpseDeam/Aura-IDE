from __future__ import annotations

from aura.config import AppSettings
from aura.gui.credits_panel import AuraCreditsPanel


class AuraPage(AuraCreditsPanel):
    """Compatibility wrapper for older imports.

    Aura Credits no longer appears in Settings; the reusable implementation now
    lives in ``aura.gui.credits_panel`` and is hosted by the standalone popout.
    """

    def collect_settings(self, settings: AppSettings) -> None:
        settings.aura_pending_session_id = self._settings.aura_pending_session_id
        settings.aura_pending_claim_secret = self._settings.aura_pending_claim_secret
