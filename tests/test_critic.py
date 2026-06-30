from types import SimpleNamespace

import aura.conversation.critic_dispatch as critic_dispatch
from aura.conversation.critic_dispatch import (
    CriticRequest,
    evaluate_deterministic_critic_request,
    parse_critic_verdict,
)
from aura.conversation.critic_verdict import CriticFinding, CriticVerdict
from aura.conversation.dispatch import WorkerDispatchRequest
from aura.roles import load_bundled_named_role_capsule


def test_named_critic_capsule_loads_with_checksum():
    capsule = load_bundled_named_role_capsule("critic", allowed={"critic"})

    assert capsule is not None
    assert capsule.name == "critic"
    assert capsule.content
    assert capsule.content == capsule.content.strip()
    assert "You are Auras invisible Critic." in capsule.content
    assert len(capsule.checksum) == 64
    assert all(char in "0123456789abcdef" for char in capsule.checksum)


def test_named_role_capsule_rejects_unsafe_and_disallowed_names():
    assert load_bundled_named_role_capsule("../critic", allowed={"critic"}) is None
    assert load_bundled_named_role_capsule("critic-name", allowed={"critic"}) is None
    assert load_bundled_named_role_capsule("critic.md", allowed={"critic"}) is None
    assert load_bundled_named_role_capsule("critic", allowed={"planner"}) is None


def test_critic_system_prompt_uses_bundled_capsule():
    capsule = load_bundled_named_role_capsule("critic", allowed={"critic"})

    assert capsule is not None
    assert critic_dispatch._critic_system_prompt() == capsule.content


def test_critic_system_prompt_falls_back_when_capsule_missing(monkeypatch):
    monkeypatch.setattr(
        critic_dispatch,
        "load_bundled_named_role_capsule",
        lambda name, allowed=None: None,
    )

    assert (
        critic_dispatch._critic_system_prompt()
        == critic_dispatch._FALLBACK_CRITIC_SYSTEM_PROMPT
    )


def test_critic_messages_use_prompt_helper(monkeypatch):
    monkeypatch.setattr(
        critic_dispatch,
        "load_bundled_named_role_capsule",
        lambda name, allowed=None: SimpleNamespace(content="patched critic capsule"),
    )
    request = CriticRequest(
        original_request=WorkerDispatchRequest(
            goal="Update behavior",
            files=["a.py"],
            spec="Make the focused change.",
            acceptance="The change is present.",
        ),
        diff_text="",
    )

    messages = critic_dispatch._critic_messages(request)

    assert messages[0] == {"role": "system", "content": "patched critic capsule"}


def test_critic_verdict_drops_findings_without_clause_and_releases():
    verdict = CriticVerdict(
        conforms=False,
        route="worker",
        findings=[
            CriticFinding(
                clause="",
                file="a.py",
                message="Vague concern.",
                suggested_action="Patch it.",
            )
        ],
        instruction="Patch it.",
    )

    assert verdict.findings == []
    assert verdict.conforms is True
    assert verdict.route == "release"
    assert verdict.instruction == ""


def test_deterministic_critic_missing_expected_symbol_routes_worker():
    request = WorkerDispatchRequest(
        goal="Add public helper",
        files=["a.py"],
        spec="Expose run_report.",
        acceptance="run_report is available to import.",
        expected_public_symbols=["run_report"],
    )
    critic_request = CriticRequest(
        original_request=request,
        diff_text="""diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1,2 @@
+def other():
+    return None
""",
    )

    verdict = evaluate_deterministic_critic_request(critic_request)

    assert verdict is not None
    assert verdict.route == "worker"
    assert verdict.conforms is False
    assert verdict.findings[0].clause == "expected_public_symbols: run_report"


def test_parse_critic_verdict_strips_markdown_fence():
    verdict = parse_critic_verdict(
        """```json
{"conforms": true, "route": "release", "findings": []}
```"""
    )

    assert verdict.conforms is True
    assert verdict.route == "release"
