# imports from std lib
import imaplib, email, re, time, os

IMAP_HOST = "imap.gmail.com"
IMAP_USER = os.environ.get('MERCHANT_PAY_IMAP_USER', '')
IMAP_PASS = os.environ.get('MERCHANT_PAY_IMAP_PASS', '')

CODE_RE = re.compile(r"\b(\d{6})\b")

def FetchCode(timeout=120, poll=5):
    end = time.time() + timeout
    with imaplib.IMAP4_SSL(IMAP_HOST) as M:
        M.login(IMAP_USER, IMAP_PASS)
        while time.time() < end:
            M.select("INBOX")
            # tweak the search to narrow sender/subject
            typ, data = M.search(None, 'UNSEEN OR SUBJECT "verification" SUBJECT "code"')
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

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    print(FetchCode())