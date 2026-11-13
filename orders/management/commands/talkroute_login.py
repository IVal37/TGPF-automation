# imports from std lib
import subprocess

# imports from django
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

# imports from selenium
from selenium.webdriver.common.selenium_manager import SeleniumManager

# imports from project
from orders.services.scrapers.talkroute import TEXT_MESSAGES_URL

# Chrome hands a second launch over to the Chrome that already owns the profile and exits at
# once. If ours exits sooner than this, it was not really started.
STARTED_AFTER_SECONDS = 3


def chrome_binary() -> str:
    """The Chrome the scraper uses (the one Selenium Manager downloaded)."""
    return SeleniumManager().binary_paths(["--browser", "chrome"])["browser_path"]


class Command(BaseCommand):
    help = (
        "Open TalkRoute in the scraper's saved Chrome profile so you can log in by hand, including "
        "Cloudflare's 'verify you are human' check. Close the window when you are done: the login "
        "stays saved, and the scraper uses it to send texts."
    )

    def handle(self, *args, **options):
        profile = settings.TALKROUTE_PROFILE_DIR
        # An ordinary Chrome, not one started by Selenium: Cloudflare's check recognises a
        # browser that is being automated and will not let a person through it.
        chrome = subprocess.Popen(
            [
                chrome_binary(),
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                TEXT_MESSAGES_URL,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.stdout.write(
            "TalkRoute is open. Log in (tick the Cloudflare box if it asks), wait until you can see "
            "your messages, then CLOSE THE WINDOW. Closing it is what saves the login."
        )
        try:
            chrome.wait(timeout=STARTED_AFTER_SECONDS)
        except subprocess.TimeoutExpired:
            chrome.wait()  # still running: wait for the person to close it
        else:
            raise CommandError(
                "Chrome closed straight away. The profile can only be used by one Chrome at a time, "
                "so make sure no other TalkRoute window is open and that the browser worker is not "
                "in the middle of sending a text, then try again."
            )
        self.stdout.write(f"Saved. The scraper will use the login stored in {profile}")
