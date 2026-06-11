"""Security blocker — blocks sensitive apps from Jarvis control."""

from __future__ import annotations

BLOCKED_APPS = {
    # Banks (Pakistan)
    "hbl", "habib bank", "meezan", "meezan bank", "ubl", "mcb", "allied bank",
    "faysal", "standard chartered", "askari", "bank alfalah", "js bank",
    "bankislami", "dubai islamic", "silkbank",

    # Payment
    "jazzcash", "easypaisa", "sadapay", "nayapay", "konnect", "hbl konnect",

    # Password managers
    "lastpass", "1password", "bitwarden", "dashlane", "keepass", "nordpass",
    "roboform", "enpass",

    # Crypto
    "binance", "coinbase", "trust wallet", "metamask", "exodus",

    # Authenticators
    "google authenticator", "microsoft authenticator", "authy",
}

BLOCKED_KEYWORDS = {
    "password", "pin code", "cvv", "otp", "atm", "credit card number",
    "card number", "banking", "bank login",
}


class SecurityBlocker:
    """Blocks sensitive operations."""

    @staticmethod
    def is_app_blocked(app_name: str) -> bool:
        """Check if app is in blocked list."""
        if not app_name:
            return False
        lower = app_name.lower().strip()
        for blocked in BLOCKED_APPS:
            if blocked in lower:
                return True
        return False

    @staticmethod
    def has_sensitive_keyword(text: str) -> bool:
        """Check if text contains sensitive keywords."""
        if not text:
            return False
        lower = text.lower()
        for kw in BLOCKED_KEYWORDS:
            if kw in lower:
                return True
        return False

    @staticmethod
    def validate_action(action: dict) -> tuple[bool, str]:
        """Validate action. Returns (allowed, reason)."""
        action_type = action.get("action", "")
        params = action.get("params", {})

        app = params.get("app", "")
        if SecurityBlocker.is_app_blocked(app):
            return False, f"'{app}' ek sensitive app hai — Jarvis ise touch nahi karega. Security ke liye."

        if action_type in ("send_email", "send_teams_message", "send_whatsapp_message"):
            message = params.get("message", "") or params.get("body", "")
            if SecurityBlocker.has_sensitive_keyword(message):
                return False, "Is message mein sensitive info (password/PIN/OTP) hai — bhejna safe nahi."

        return True, ""
