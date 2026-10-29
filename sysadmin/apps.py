from django.apps import AppConfig


class SysadminConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "sysadmin"

    def ready(self):
        from . import signals  # noqa: F401 — connects the login-event receivers
        # Backup rows (real data copied from live) are Ian's to delete, on
        # every table — see sysadmin.backup. The admin has registered
        # everything by now: django.contrib.admin is earlier in INSTALLED_APPS.
        from .backup import guard_admin_deletes
        guard_admin_deletes()
