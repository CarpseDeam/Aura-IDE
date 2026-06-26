"""Intent-based policy classifier for browse drone actions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PolicyResult:
    verdict: str  # "allow", "needs_planner_decision", "deny_manual"
    reason: str  # human-readable explanation
    matched_text: str | None = None  # the phrase/pattern that triggered


CONSEQUENTIAL_PHRASES: tuple[str, ...] = (
    "place order", "sign out", "connect account", "cancel order", "change settings",
    "submit", "send", "post", "save", "delete", "remove", "purchase", "buy", "checkout",
    "confirm", "unsubscribe", "logout", "upload", "pay", "subscribe",
    "register", "create account", "sign in", "log in",
)

DENY_MANUAL_PATTERNS: tuple[str, ...] = (
    "password", "card number", "credit card", "card no", "cvv", "cvc",
    "ssn", "social security", "routing number", "account number",
    "mfa", "two-factor", "two factor", "2fa", "authenticator", "security code",
    "delete account", "close account", "cancel account",
    "terms of service", "terms and conditions",
)


def classify_action(
    action_type: str,
    candidate,
    current_url: str,
    page_title: str,
    allowed_consequential_actions: list[str] | None = None,
) -> PolicyResult:
    """Classify a browse action against the policy.

    Ordered priority:
    1. deny_manual — password fields and sensitive patterns
    2. needs_planner_decision — consequential actions not pre-approved
    3. allow — default for everything else
    """
    # --- 1. deny_manual ---
    if action_type == "fill":
        input_type = getattr(candidate, "input_type", "") or ""
        if input_type.lower() == "password":
            return PolicyResult(
                "deny_manual", "Password field \u2014 too sensitive to automate"
            )

    label_lower = (getattr(candidate, "label", "") or "").lower()
    href_lower = (getattr(candidate, "href", "") or "").lower()

    for phrase in DENY_MANUAL_PATTERNS:
        if phrase in label_lower or phrase in href_lower:
            return PolicyResult(
                "deny_manual",
                f"Sensitive action blocked \u2014 '{phrase}' requires manual handling",
                phrase,
            )

    # --- 2. needs_planner_decision ---
    allowed = allowed_consequential_actions or []
    for phrase in CONSEQUENTIAL_PHRASES:
        if phrase in label_lower or phrase in href_lower:
            for allowed_phrase in allowed:
                if (
                    allowed_phrase.lower() in label_lower
                    or allowed_phrase.lower() in href_lower
                ):
                    return PolicyResult(
                        "allow",
                        f"Consequential action allowed by permissions: '{phrase}'",
                        phrase,
                    )
            return PolicyResult(
                "needs_planner_decision",
                f"Consequential action \u2014 '{phrase}' requires planner decision",
                phrase,
            )

    # --- 3. allow ---
    return PolicyResult("allow", "")
