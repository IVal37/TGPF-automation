# imports from std lib
import imaplib, email, re, time, os

IMAP_HOST = "imap.gmail.com"

# sender of GBX MerchantPay's "One-time verification code" email
CODE_SENDER = "donotreply@greenbaxmarketplace.io"

CODE_RE = re.compile(r"\b(\d{6})\b")

def FetchCode(timeout=120, poll=5):
    # read when called, not at import, so a .env loaded after this module is imported still counts
    imap_user = os.environ.get('MERCHANT_PAY_IMAP_USER', '')
    imap_pass = os.environ.get('MERCHANT_PAY_IMAP_PASS', '')
    end = time.time() + timeout
    with imaplib.IMAP4_SSL(IMAP_HOST) as M:
        M.login(imap_user, imap_pass)
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

