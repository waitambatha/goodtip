from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.dispatch import receiver

from .models import LoginEvent


def _client_ip(request):
    if request is None:
        return None
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    return xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR")


def _user_agent(request):
    if request is None:
        return ""
    return request.META.get("HTTP_USER_AGENT", "")[:300]


@receiver(user_logged_in)
def record_login_success(sender, request, user, **kwargs):
    LoginEvent.objects.create(
        user=user, email=user.email, success=True,
        ip_address=_client_ip(request), user_agent=_user_agent(request),
    )


@receiver(user_login_failed)
def record_login_failure(sender, credentials, request=None, **kwargs):
    LoginEvent.objects.create(
        email=(credentials.get("username") or "")[:254], success=False,
        ip_address=_client_ip(request), user_agent=_user_agent(request),
    )
