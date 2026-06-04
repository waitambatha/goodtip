from django.conf import settings
from django.core import signing


JOIN_SALT = "orgs.join_link"


def make_join_token(org_id: int) -> str:
    return signing.dumps({"org_id": org_id}, salt=JOIN_SALT)


def parse_join_token(token: str) -> int | None:
    max_age = settings.JOIN_LINK_MAX_AGE_DAYS * 24 * 60 * 60
    try:
        data = signing.loads(token, salt=JOIN_SALT, max_age=max_age)
        return int(data["org_id"])
    except (signing.BadSignature, signing.SignatureExpired, KeyError, ValueError, TypeError):
        return None
