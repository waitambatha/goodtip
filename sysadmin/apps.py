from django.apps import AppConfig


class SysadminConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "sysadmin"

    def ready(self):
        from . import signals  # noqa: F401 — connects the login-event receivers
