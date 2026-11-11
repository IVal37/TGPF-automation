# imports from std lib
import email
import imaplib
import re
import time

from django.conf import settings

IMAP_HOST = "imap.gmail.com"

# sender of GBX MerchantPay's "One-time verification code" email
CODE_SENDER = "donotreply@greenbaxmarketplace.io"

CODE_RE = re.compile(r"\b(\d{6})\b")


def fetch_2fa_code(timeout: int = 120, poll: int = 5) -> str:
    end = time.time() + timeout
    with imaplib.IMAP4_SSL(IMAP_HOST) as M:
        M.login(settings.MERCHANT_PAY_IMAP_USER, settings.MERCHANT_PAY_IMAP_PASS)
        while time.time() < end:
            M.select("INBOX")
            typ, data = M.search(None, f'UNSEEN FROM "{CODE_SENDER}"')
            ids = data[0].split()
            if ids:
                # look at newest first
                for msg_id in reversed(ids):
                    typ, msg_data = M.fetch(msg_id, "(RFC822)")
                    msg = email.message_from_bytes(msg_data[0][1])
                    # get plain text
                    parts = [msg.get("Subject", "")]
                    if msg.is_multipart():
                        for p in msg.walk():
                            if p.get_content_type() == "text/plain":
                                parts.append(p.get_payload(decode=True).decode(errors="ignore"))
                    else:
                        parts.append(msg.get_payload(decode=True).decode(errors="ignore"))
                    text = "\n".join(parts)
                    m = CODE_RE.search(text)
                    if m:
                        return m.group(1)
            time.sleep(poll)
    raise TimeoutError("No 2FA code found")
