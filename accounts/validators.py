import re

from django.core.exceptions import ValidationError


class PasswordComplexityValidator:
    """Require at least one uppercase letter, lowercase letter, number and symbol.

    Length is handled separately by MinimumLengthValidator. The requirement keys
    mirror the data-req values in templates/partials/pw_checklist.html so the
    live UI checklist and server enforcement can't drift apart silently.
    """

    REQUIREMENTS = [
        ("upper", r"[A-Z]", "one uppercase letter"),
        ("lower", r"[a-z]", "one lowercase letter"),
        ("digit", r"[0-9]", "one number"),
        ("symbol", r"[^A-Za-z0-9]", "one symbol (e.g. ! @ # $ %)"),
    ]

    def validate(self, password, user=None):
        missing = [
            label
            for _key, pattern, label in self.REQUIREMENTS
            if not re.search(pattern, password)
        ]
        if missing:
            raise ValidationError(
                "Your password must contain at least %s." % ", ".join(missing),
                code="password_missing_complexity",
            )

    def get_help_text(self):
        return (
            "Your password must contain at least one uppercase letter, "
            "one lowercase letter, one number and one symbol."
        )
