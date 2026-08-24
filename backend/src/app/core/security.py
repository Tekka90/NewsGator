"""Auth helpers: argon2 password hashing + signed session cookies."""

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import BadSignature, URLSafeSerializer

from app.core.config import settings

_ph = PasswordHasher()
SESSION_COOKIE = "newsgator_session"


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(settings.secret_key, salt="newsgator-session")


def make_session_token(user_id: int) -> str:
    return _serializer().dumps({"uid": user_id})


def parse_session_token(token: str) -> int | None:
    try:
        data = _serializer().loads(token)
    except BadSignature:
        return None
    uid = data.get("uid")
    return int(uid) if isinstance(uid, int) else None
