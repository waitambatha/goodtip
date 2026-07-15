from django.core.management.base import BaseCommand
from accounts.models import User


class Command(BaseCommand):
    help = "Update a user's email address"

    def add_arguments(self, parser):
        parser.add_argument("username", type=str, help="Username or current email")
        parser.add_argument("new_email", type=str, help="New email address")

    def handle(self, *args, **options):
        identifier = options["username"]
        new_email = options["new_email"]

        try:
            user = User.objects.get(email=identifier)
            old_email = user.email
            user.email = new_email
            user.username = new_email
            user.save()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully updated {identifier}: {old_email} → {new_email}"
                )
            )
        except User.DoesNotExist:
            self.stdout.write(
                self.style.ERROR(f"User with email '{identifier}' not found")
            )
