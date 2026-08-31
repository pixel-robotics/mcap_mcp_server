"""Google Workspace authentication for the remote (HTTP) transport.

The server fronts Google with fastmcp's OAuth proxy (``GoogleProvider``):
MCP clients discover the server via the standard MCP OAuth flow, the person
logs in at Google in their browser, and every subsequent request is verified
against Google's tokeninfo endpoint.

A valid Google account alone is not enough, though — ``WorkspaceTokenVerifier``
additionally rejects any account whose verified e-mail address is not on one of
the configured Workspace domains. That check happens at token verification, so
an outside account never gets a working session.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from fastmcp.server.auth.providers.google import GoogleProvider, GoogleTokenVerifier

from mcap_mcp_server.config import ServerConfig

logger = logging.getLogger(__name__)


class AuthConfigError(RuntimeError):
    """Raised when the server would start with an unusable auth configuration."""


class WorkspaceTokenVerifier(GoogleTokenVerifier):
    """Google token verifier that only accepts configured Workspace domains."""

    def __init__(self, *, allowed_domains: Iterable[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self.allowed_domains = {
            d.strip().lstrip("@").lower() for d in allowed_domains if d and d.strip()
        }
        if not self.allowed_domains:
            raise AuthConfigError("WorkspaceTokenVerifier needs at least one allowed domain")

    async def verify_token(self, token: str):
        access = await super().verify_token(token)
        if access is None:
            return None
        claims = access.claims or {}
        email = str(claims.get("email") or "")
        domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
        if claims.get("email_verified") not in (True, "true", "True", "1", 1):
            logger.warning("Rejected Google token for %r: e-mail not verified", email)
            return None
        if domain not in self.allowed_domains:
            logger.warning(
                "Rejected Google login from %r: not in allowed domains %s",
                email,
                sorted(self.allowed_domains),
            )
            return None
        return access


class GoogleWorkspaceProvider(GoogleProvider):
    """GoogleProvider whose token verification is restricted to Workspace domains."""

    def __init__(self, *, allowed_domains: Iterable[str], timeout_seconds: int = 10, **kwargs):
        domains = [d.strip().lstrip("@").lower() for d in allowed_domains if d and d.strip()]
        if not domains:
            raise AuthConfigError("At least one allowed Google Workspace domain is required")

        # The e-mail scope is what lets the verifier see the account's domain.
        kwargs.setdefault("required_scopes", ["openid", "email"])
        extra = dict(kwargs.pop("extra_authorize_params", None) or {})
        if len(domains) == 1:
            # Preselects the Workspace in Google's account picker. UX only —
            # enforcement lives in WorkspaceTokenVerifier below.
            extra.setdefault("hd", domains[0])
        super().__init__(
            timeout_seconds=timeout_seconds,
            extra_authorize_params=extra or None,
            **kwargs,
        )

        base_verifier = getattr(self, "_token_validator", None)
        if not isinstance(base_verifier, GoogleTokenVerifier):
            # Fail closed: without the swap below the domain restriction would
            # silently not exist.
            raise AuthConfigError(
                "fastmcp internals changed: GoogleProvider no longer keeps its "
                "verifier in _token_validator, so the Workspace domain check "
                "cannot be installed. Pin fastmcp<4 or update mcap_mcp_server.auth."
            )
        self._token_validator = WorkspaceTokenVerifier(
            allowed_domains=domains,
            required_scopes=base_verifier.required_scopes,
            timeout_seconds=timeout_seconds,
        )

    @property
    def allowed_domains(self) -> set[str]:
        return self._token_validator.allowed_domains


def build_auth(config: ServerConfig) -> GoogleWorkspaceProvider | None:
    """Return the auth provider for the configured transport.

    stdio needs no auth — the MCP client owns the process. HTTP transports are
    reachable over the network, so they refuse to start without Google auth
    unless insecure_no_auth is set explicitly.
    """
    if config.transport == "stdio":
        return None

    if not config.google_client_id:
        if config.insecure_no_auth:
            logger.warning(
                "Serving over %s WITHOUT authentication — anyone who can reach "
                "port %d can read your recordings and your Foxglove data.",
                config.transport,
                config.sse_port,
            )
            return None
        raise AuthConfigError(
            "Refusing to serve over HTTP without authentication. Set "
            "MCAP_GOOGLE_CLIENT_ID / MCAP_GOOGLE_CLIENT_SECRET / "
            "MCAP_ALLOWED_GOOGLE_DOMAINS / MCAP_BASE_URL (or the [auth] section "
            "in mcap-mcp-server.toml) to enable Google Workspace login, or set "
            "MCAP_INSECURE_NO_AUTH=true if you really want an open server."
        )

    missing = [
        name
        for name, value in (
            ("google_client_secret", config.google_client_secret),
            ("allowed_google_domains", config.allowed_google_domains),
            ("base_url", config.base_url),
        )
        if not value
    ]
    if missing:
        raise AuthConfigError(
            "Google auth is enabled (google_client_id is set) but incomplete — "
            f"missing: {', '.join(missing)}. base_url must be the public URL of "
            "this server (Google redirects the browser back to "
            "<base_url>/auth/callback), and allowed_google_domains lists the "
            "Workspace domains that may log in, e.g. 'lvairo.com'."
        )

    return GoogleWorkspaceProvider(
        client_id=config.google_client_id,
        client_secret=config.google_client_secret,
        base_url=config.base_url,
        allowed_domains=config.allowed_google_domains,
    )
