from django.conf import settings
from django.core import signing


JOIN_SALT = "orgs.join_link"


def make_join_token(org_id: int, inviter_id: int | None = None) -> str:
    payload = {"org_id": org_id}
    if inviter_id is not None:
        payload["inviter_id"] = inviter_id
    return signing.dumps(payload, salt=JOIN_SALT)


def parse_join_token(token: str) -> dict | None:
    """Return {"org_id": int, "inviter_id": int | None} or None if invalid."""
    max_age = settings.JOIN_LINK_MAX_AGE_DAYS * 24 * 60 * 60
    try:
        data = signing.loads(token, salt=JOIN_SALT, max_age=max_age)
        org_id = int(data["org_id"])
    except (signing.BadSignature, signing.SignatureExpired, KeyError, ValueError, TypeError):
        return None
    raw_inviter = data.get("inviter_id")
    try:
        inviter_id = int(raw_inviter) if raw_inviter is not None else None
    except (ValueError, TypeError):
        inviter_id = None
    return {"org_id": org_id, "inviter_id": inviter_id}
