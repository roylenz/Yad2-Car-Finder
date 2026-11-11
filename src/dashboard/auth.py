"""Cloudflare Access login check.

Cloudflare Access sits in front of the tunnel and handles sign-in. Every request it forwards
carries a signed JWT (`Cf-Access-Jwt-Assertion` header). We verify it, so a request that
reaches the app without going through Access (e.g. from the LAN) is refused.

Env: `CF_ACCESS_TEAM_DOMAIN` (e.g. `myteam.cloudflareaccess.com`), `CF_ACCESS_AUD` (the
application's Audience tag). `DASHBOARD_AUTH=off` disables the check for local development.
"""

import os
from dataclasses import dataclass

import jwt


class AccessDenied(Exception):
    pass


@dataclass
class CloudflareAccess:
    team_domain: str
    audience: str

    def __post_init__(self) -> None:
        self.team_domain = self.team_domain.removeprefix("https://").rstrip("/")
        self._keys = jwt.PyJWKClient(f"https://{self.team_domain}/cdn-cgi/access/certs")

    def verify(self, token: str | None) -> str:
        """Return the signed-in email, or raise AccessDenied."""
        if not token:
            raise AccessDenied("Missing Cloudflare Access token")
        try:
            key = self._keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=f"https://{self.team_domain}",
            )
        except jwt.PyJWTError as error:
            raise AccessDenied(f"Invalid Cloudflare Access token: {error}") from error
        return claims.get("email") or claims.get("sub") or "unknown"


class NoAuth:
    def verify(self, token: str | None) -> str:
        return "local"


class Unconfigured:
    """Fail closed when auth is on but not configured."""

    def verify(self, token: str | None) -> str:
        raise AccessDenied(
            "Dashboard login is not configured: set CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD "
            "(or DASHBOARD_AUTH=off for local development)"
        )


def from_env():
    if os.getenv("DASHBOARD_AUTH", "cloudflare").lower() == "off":
        return NoAuth()
    team, audience = os.getenv("CF_ACCESS_TEAM_DOMAIN"), os.getenv("CF_ACCESS_AUD")
    if not team or not audience:
        return Unconfigured()
    return CloudflareAccess(team, audience)
