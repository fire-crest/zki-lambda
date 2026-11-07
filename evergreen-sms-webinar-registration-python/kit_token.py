"""
Create and check subscriber tokens (Kit custom field 'indiv_token').

A token looks like:  Yq3s0V8m2b1KfQx7Wd9pLr4tHn6uJc5eAa
It contains the Kit subscriber ID, encrypted. Nobody can read the ID from the
token, and nobody can make or alter a token without the secret key.

Encryption is AES-SIV (RFC 5297), from the widely used 'cryptography' library:
  - Authenticated: any change to a token is detected, and the token is rejected.
  - Deterministic: the same subscriber always gets the same token, so re-running
    the webhook or the backfill doesn't change anyone's token. The only thing an
    observer could learn is that two identical tokens belong to the same person.

The secret key is 64 random bytes, stored base64url-encoded in Parameter Store.
Make one with:
  python3 -c "import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(64)).decode())"

Used by:
  - kit-set-indiv-token (creates tokens when Kit's webhook fires)
  - the replay tagging function (checks tokens sent from the replay page)
  - backfill_tokens.py (catches up subscribers missing a token)

If you ever need to invalidate every token (e.g. the key leaks), put a new key
in Parameter Store and run backfill_tokens.py --apply.
"""

import base64
import binascii
import functools
import string

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESSIV

# Ties every token to this purpose and format version. If the format ever
# changes, bump the version so old and new tokens can't be confused.
TOKEN_CONTEXT = [b"indiv_token:v1"]

# Kit subscriber IDs are numbers; 20 digits leaves plenty of headroom
MAX_ID_LENGTH = 20

# A 20-digit ID makes a 48-character token; refuse anything much longer unread
MAX_TOKEN_LENGTH = 64
TOKEN_CHARACTERS = frozenset(string.ascii_letters + string.digits + "-_")


def is_valid_subscriber_id(subscriber_id):
    """True if subscriber_id looks like a Kit subscriber ID (plain ASCII digits)."""
    sid = str(subscriber_id)
    return 0 < len(sid) <= MAX_ID_LENGTH and sid.isascii() and sid.isdigit()


def make_token(subscriber_id, secret):
    """Return the token for this subscriber ID."""
    sid = str(subscriber_id)
    if not is_valid_subscriber_id(sid):
        raise ValueError(f"Not a valid Kit subscriber ID: {subscriber_id!r}")
    encrypted = _cipher(secret).encrypt(sid.encode("ascii"), TOKEN_CONTEXT)
    return base64.urlsafe_b64encode(encrypted).rstrip(b"=").decode("ascii")


def read_token(token, secret):
    """
    Return the subscriber ID if the token is genuine, otherwise None.
    Never raises on a bad token, so it's safe to call on anything a web page sends us.
    """
    if not isinstance(token, str) or not 0 < len(token) <= MAX_TOKEN_LENGTH:
        return None
    if not set(token) <= TOKEN_CHARACTERS:
        return None
    cipher = _cipher(secret)  # outside the try: a bad secret is our fault and should fail loudly
    try:
        encrypted = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        sid = cipher.decrypt(encrypted, TOKEN_CONTEXT).decode("ascii")
    except (InvalidTag, ValueError, binascii.Error, UnicodeDecodeError):
        return None
    return sid if is_valid_subscriber_id(sid) else None


@functools.lru_cache(maxsize=4)
def _cipher(secret):
    try:
        key = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
    except (binascii.Error, ValueError) as error:
        raise ValueError("Token secret is not valid base64url") from error
    if len(key) != 64:
        raise ValueError(f"Token secret must decode to 64 bytes, got {len(key)}")
    return AESSIV(key)
