import json
import sys

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from sysadmin.models import StressTestRun

FIELD_NAMES = (
    "label", "target", "started_at", "finished_at", "total_requests",
    "success_count", "failure_count", "avg_response_ms", "p95_response_ms",
    "max_response_ms", "requests_per_sec", "notes",
)


def _parse_when(raw):
    if not raw:
        return None
    when = parse_datetime(raw)
    if when is None:
        raise ValueError(f"Could not parse timestamp: {raw!r}")
    if timezone.is_naive(when):
        when = timezone.make_aware(when, timezone.get_current_timezone())
    return when


class Command(BaseCommand):
    help = (
        "Record the result of a load/stress test run into StressTestRun, so it "
        "shows up in /admin/ and on the system report page. Reads a JSON "
        "payload from --file or stdin: {label, target, started_at, "
        "finished_at, total_requests, success_count, failure_count, "
        "avg_response_ms, p95_response_ms, max_response_ms, requests_per_sec, "
        "notes, ...anything else lands in raw_results}."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--file", help="Path to a JSON file. Reads stdin if omitted.",
        )

    def handle(self, *args, **options):
        raw = (
            open(options["file"]).read() if options["file"] else sys.stdin.read()
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise CommandError(f"Invalid JSON: {e}")

        if not payload.get("label"):
            raise CommandError("Payload must include a 'label'.")

        try:
            started_at = _parse_when(payload.get("started_at")) or timezone.now()
            finished_at = _parse_when(payload.get("finished_at"))
        except ValueError as e:
            raise CommandError(str(e))

        known = {k: payload[k] for k in FIELD_NAMES if k in payload and k not in ("started_at", "finished_at")}
        extra = {k: v for k, v in payload.items() if k not in FIELD_NAMES}

        run = StressTestRun.objects.create(
            started_at=started_at, finished_at=finished_at,
            raw_results=extra, **known,
        )
        self.stdout.write(self.style.SUCCESS(
            f"Recorded StressTestRun #{run.pk} — {run.label} ({run.total_requests} requests)"
        ))
