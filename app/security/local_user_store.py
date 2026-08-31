"""Postgres-backed registry of locally-provisioned human accounts --
Compliance_Officer/System_Admin users who authenticate directly against
this service (email + password) rather than via an external SSO IdP.

Backed by `app.db.models.User` via the ordinary (non-tenant-scoped)
session factory in `app.db.session` -- human accounts are deliberately
NOT tenant-partitioned, unlike the rest of this schema (see `User`'s
docstring), so this store never goes through
`app.db.tenant_session.get_tenant_db_session`'s RLS-scoped session.

Passwords are stored as salted bcrypt hashes -- never plaintext, and
never round-tripped through the ORM as anything but the hash.
"""
from __future__ import annotations

import uuid

import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.security.models import LocalUser, Role

# A fixed, never-matching bcrypt hash verified against on a lookup miss, so
# an unknown email takes the same code path (and roughly the same
# wall-clock time) as a wrong-password hit -- see `authenticate` below.
_DUMMY_HASH = bcrypt.hashpw(b"dummy-constant-time-comparison", bcrypt.gensalt())


class EmailAlreadyRegisteredError(Exception):
    """Raised by `register` when the email is already taken -- the caller
    (POST /v1/auth/signup) maps this to 400 Bad Request."""


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def _verify_password(password: str, hashed: bytes | str) -> bool:
    hashed_bytes = hashed.encode("ascii") if isinstance(hashed, str) else hashed
    return bcrypt.checkpw(password.encode("utf-8"), hashed_bytes)


def _to_local_user(row: User) -> LocalUser:
    return LocalUser(
        email=row.email,
        password_hash=row.password_hash,
        roles=[Role(r) for r in row.roles],
        disabled=row.disabled,
    )


class LocalUserStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _get_row(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email.strip().lower())
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def register(self, email: str, password: str, roles: list[Role] | None = None) -> str:
        """Creates a new local account. Called from an operator-facing admin
        route (System_Admin-provisioned Compliance_Officer accounts, see
        POST /v1/auth/users) or the public signup endpoint (POST
        /v1/auth/signup). Raises `EmailAlreadyRegisteredError` if the email
        is already taken -- checked explicitly (rather than relying solely
        on the unique constraint) so the caller gets a clean typed error
        instead of an IntegrityError leaking out of a half-flushed session.
        Returns the new account's business-key `user_id`."""
        normalized_email = email.strip().lower()
        if await self._get_row(normalized_email) is not None:
            raise EmailAlreadyRegisteredError(f"Email already registered: {normalized_email}")

        user_id = str(uuid.uuid4())
        row = User(
            user_id=user_id,
            email=normalized_email,
            password_hash=_hash_password(password),
            roles=[r.value for r in (roles or [Role.COMPLIANCE_OFFICER])],
        )
        self._session.add(row)
        await self._session.flush()
        return user_id

    async def authenticate(self, email: str, password: str) -> LocalUser | None:
        """Returns the LocalUser iff email exists, is not disabled, and
        password matches its stored hash. Returns None (never raises) on
        any failure -- constant-shape response so a caller cannot
        distinguish "unknown email" from "wrong password" through a
        different exception path, which would leak which emails are
        registered."""
        row = await self._get_row(email)
        if row is None:
            # Still run a bcrypt comparison against the dummy hash so a
            # nonexistent email doesn't respond measurably faster than a
            # wrong-password one (timing side-channel on email enumeration).
            _verify_password(password, _DUMMY_HASH)
            return None

        if row.disabled or not _verify_password(password, row.password_hash):
            return None
        return _to_local_user(row)

    async def disable(self, email: str) -> None:
        row = await self._get_row(email)
        if row is None:
            return
        row.disabled = True
        await self._session.flush()
