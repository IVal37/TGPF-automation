# imports from django
from django.core.management.base import BaseCommand

# imports from project
from orders.models import City, TravelTime
from orders.services import travel


class Command(BaseCommand):
    help = (
        "Fetch driving times between every pair of delivery cities from Google Maps "
        "(needs GOOGLE_MAPS_API_KEY). Only missing, unconfirmed or stale pairs unless --force."
    )

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="refetch every pair, even fresh ones")

    def handle(self, *args, **options):
        if not travel.api_key():
            self.stdout.write("GOOGLE_MAPS_API_KEY is not set; nothing to do (the stored travel times are used).")
            return

        names = sorted(City.objects.values_list("name", flat=True))
        existing = {(r.city_a, r.city_b): r for r in TravelTime.objects.all()}

        # one request per origin city: that city against up to 25 others
        wanted = {}
        fresh = 0
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                if options["force"] or travel.needs_refresh(existing.get((a, b))):
                    wanted.setdefault(a, []).append(b)
                else:
                    fresh += 1

        updated = failed = 0
        for origin, destinations in wanted.items():
            for start in range(0, len(destinations), travel.MAX_DESTINATIONS):
                chunk = destinations[start:start + travel.MAX_DESTINATIONS]
                try:
                    answers = travel.fetch_minutes(origin, chunk)
                except Exception as e:
                    failed += len(chunk)
                    self.stderr.write(f"{origin}: request failed: {type(e).__name__}: {travel._scrub(str(e))}")
                    continue
                for destination in chunk:
                    if destination in answers:
                        travel.store_google_minutes(origin, destination, answers[destination])
                        updated += 1
                    else:
                        failed += 1
                        self.stderr.write(f"{origin} - {destination}: no driving route returned")

        self.stdout.write(f"Updated {updated} pair(s), {failed} failed, {fresh} already fresh.")
