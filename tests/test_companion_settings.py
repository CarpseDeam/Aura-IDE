"""Tests for Companion settings — defaults, migration, dev override, and pairing."""

import os
from unittest.mock import patch

from aura.companion.defaults import (
    DEFAULT_HOSTED_COMPANION_RELAY_URL,
    DEFAULT_HOSTED_COMPANION_WEB_URL,
    DEFAULT_LOCAL_COMPANION_RELAY_URL,
    DEFAULT_LOCAL_COMPANION_WEB_URL,
)
from aura.companion.local_relay import is_local_relay_url, normalize_relay_url
from aura.settings import AppSettings


class TestAppSettingsDefaults:
    """Fresh AppSettings uses hosted defaults."""

    def test_fresh_settings_use_hosted_defaults(self):
        s = AppSettings()
        assert s.companion_relay_url == DEFAULT_HOSTED_COMPANION_RELAY_URL
        assert s.companion_web_url == DEFAULT_HOSTED_COMPANION_WEB_URL


class TestMigration:
    """from_dict() migrates old localhost defaults to hosted."""

    def test_empty_data_migrates_to_hosted(self):
        s = AppSettings.from_dict({})
        assert s.companion_relay_url == DEFAULT_HOSTED_COMPANION_RELAY_URL
        assert s.companion_web_url == DEFAULT_HOSTED_COMPANION_WEB_URL

    def test_old_localhost_data_migrates_to_hosted(self):
        s = AppSettings.from_dict({
            "companion_relay_url": "ws://localhost:8765",
            "companion_web_url": "http://localhost:5173",
        })
        assert s.companion_relay_url == DEFAULT_HOSTED_COMPANION_RELAY_URL
        assert s.companion_web_url == DEFAULT_HOSTED_COMPANION_WEB_URL

    def test_empty_string_data_migrates_to_hosted(self):
        s = AppSettings.from_dict({
            "companion_relay_url": "",
            "companion_web_url": "",
        })
        assert s.companion_relay_url == DEFAULT_HOSTED_COMPANION_RELAY_URL
        assert s.companion_web_url == DEFAULT_HOSTED_COMPANION_WEB_URL

    def test_custom_urls_are_preserved(self):
        s = AppSettings.from_dict({
            "companion_relay_url": "wss://my-relay.example.com/ws",
            "companion_web_url": "https://companion.example.com",
        })
        assert s.companion_relay_url == "wss://my-relay.example.com/ws"
        assert s.companion_web_url == "https://companion.example.com"


class TestDevOverride:
    """AURA_COMPANION_DEV_LOCAL=1 overrides to localhost defaults."""

    def test_dev_local_env_overrides_to_localhost(self):
        with patch.dict(os.environ, {"AURA_COMPANION_DEV_LOCAL": "1"}):
            s = AppSettings.from_dict({})
            assert s.companion_relay_url == DEFAULT_LOCAL_COMPANION_RELAY_URL
            assert s.companion_web_url == DEFAULT_LOCAL_COMPANION_WEB_URL

    def test_dev_local_env_overrides_old_saved_values(self):
        with patch.dict(os.environ, {"AURA_COMPANION_DEV_LOCAL": "1"}):
            s = AppSettings.from_dict({
                "companion_relay_url": "ws://localhost:8765",
                "companion_web_url": "http://localhost:5173",
            })
            assert s.companion_relay_url == DEFAULT_LOCAL_COMPANION_RELAY_URL
            assert s.companion_web_url == DEFAULT_LOCAL_COMPANION_WEB_URL

    def test_dev_local_env_overrides_custom_saved_values(self):
        """Dev env overrides even explicit saved URLs — devs want this."""
        with patch.dict(os.environ, {"AURA_COMPANION_DEV_LOCAL": "1"}):
            s = AppSettings.from_dict({
                "companion_relay_url": "wss://my-relay.example.com/ws",
                "companion_web_url": "https://companion.example.com",
            })
            assert s.companion_relay_url == DEFAULT_LOCAL_COMPANION_RELAY_URL
            assert s.companion_web_url == DEFAULT_LOCAL_COMPANION_WEB_URL


class TestStartPairingRelayParam:
    """start_pairing() conditional relay param logic."""

    def test_hosted_web_localhost_relay_skips_relay_param(self):
        """Hosted web + localhost relay → phones can't reach it, skip relay."""
        from unittest.mock import MagicMock

        from aura.companion.manager import CompanionManager

        mgr = CompanionManager.__new__(CompanionManager)
        mgr._active_relay_url = ""
        mgr._current_project_id = ""
        mgr._current_conversation_id = ""
        mgr._current_pairing_code = ""

        settings = AppSettings()
        # hosted web, but relay is localhost
        settings.companion_relay_url = DEFAULT_LOCAL_COMPANION_RELAY_URL
        settings.companion_web_url = DEFAULT_HOSTED_COMPANION_WEB_URL
        mgr._settings = settings

        mgr._ws_client = None

        # Mock the signals, send_event, generate_new_pairing_code
        mgr.connection_status_changed = MagicMock()
        mgr.connection_error = MagicMock()
        mgr.message_received = MagicMock()
        mgr.pairing_code_available = MagicMock()
        mgr.pairing_code_invalidated = MagicMock()
        mgr.pairing_complete = MagicMock()
        mgr.conversation_selected_by_companion = MagicMock()
        mgr.send_event = MagicMock()
        mgr.generate_new_pairing_code = MagicMock(return_value="ABC123")

        from unittest.mock import patch as mock_patch
        with mock_patch.object(mgr, "generate_new_pairing_code", return_value="ABC123"):
            with mock_patch("aura.companion.manager.pairing_code_expiry", return_value=1700000000.0):
                with mock_patch("aura.companion.manager.generate_ticket", return_value="ticket-xyz"):
                    with mock_patch("aura.companion.manager.get_device_id", return_value="desktop_abc"):
                        with mock_patch("aura.companion.manager.get_device_display_name", return_value="Test Desktop"):
                            result = mgr.start_pairing()

        pair_url = result["pair_url"]
        # Should NOT include relay= because web is hosted and relay is localhost
        assert "relay=" not in pair_url, f"Unexpected relay param in: {pair_url}"
        assert "ticket=ticket-xyz" in pair_url

    def test_localhost_web_localhost_relay_includes_relay_param(self):
        """Localhost web + localhost relay → phone is on same LAN, include relay."""
        from unittest.mock import MagicMock

        from aura.companion.manager import CompanionManager

        mgr = CompanionManager.__new__(CompanionManager)
        mgr._active_relay_url = ""
        mgr._current_project_id = ""
        mgr._current_conversation_id = ""
        mgr._current_pairing_code = ""

        settings = AppSettings()
        settings.companion_relay_url = DEFAULT_LOCAL_COMPANION_RELAY_URL
        settings.companion_web_url = DEFAULT_LOCAL_COMPANION_WEB_URL
        mgr._settings = settings

        mgr._ws_client = None

        mgr.connection_status_changed = MagicMock()
        mgr.connection_error = MagicMock()
        mgr.message_received = MagicMock()
        mgr.pairing_code_available = MagicMock()
        mgr.pairing_code_invalidated = MagicMock()
        mgr.pairing_complete = MagicMock()
        mgr.conversation_selected_by_companion = MagicMock()
        mgr.send_event = MagicMock()

        from unittest.mock import patch as mock_patch
        with mock_patch.object(mgr, "generate_new_pairing_code", return_value="ABC123"):
            with mock_patch("aura.companion.manager.pairing_code_expiry", return_value=1700000000.0):
                with mock_patch("aura.companion.manager.generate_ticket", return_value="ticket-xyz"):
                    with mock_patch("aura.companion.manager.get_device_id", return_value="desktop_abc"):
                        with mock_patch("aura.companion.manager.get_device_display_name", return_value="Test Desktop"):
                            result = mgr.start_pairing()

        pair_url = result["pair_url"]
        # Should include relay= because web is localhost
        assert "relay=" in pair_url, f"Missing relay param in: {pair_url}"


class TestNormalizeRelayUrl:
    """normalize_relay_url on hosted relay returns expected URL."""

    def test_hosted_relay_normalization(self):
        url = normalize_relay_url(DEFAULT_HOSTED_COMPANION_RELAY_URL)
        assert url == DEFAULT_HOSTED_COMPANION_RELAY_URL

    def test_is_local_relay_url_false_for_hosted(self):
        assert is_local_relay_url(DEFAULT_HOSTED_COMPANION_RELAY_URL) is False

    def test_is_local_relay_url_true_for_localhost(self):
        assert is_local_relay_url(DEFAULT_LOCAL_COMPANION_RELAY_URL) is True
