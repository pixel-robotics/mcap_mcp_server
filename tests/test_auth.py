"""Tests for Google Workspace authentication."""

from __future__ import annotations

import pytest
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.google import GoogleTokenVerifier

from mcap_mcp_server.auth import (
    AuthConfigError,
    GoogleWorkspaceProvider,
    WorkspaceTokenVerifier,
    build_auth,
)
from mcap_mcp_server.config import ServerConfig


def _google_token(claims: dict) -> AccessToken:
    return AccessToken(
        token="tok", client_id="sub123", scopes=["openid"], expires_at=None, claims=claims
    )


def _patch_google_verify(monkeypatch, claims_or_none):
    """Make the upstream Google verification return a canned result."""

    async def fake_verify(self, token):
        if claims_or_none is None:
            return None
        return _google_token(claims_or_none)

    monkeypatch.setattr(GoogleTokenVerifier, "verify_token", fake_verify)


# ---------------------------------------------------------------------------
# WorkspaceTokenVerifier
# ---------------------------------------------------------------------------


class TestWorkspaceTokenVerifier:
    async def test_accepts_allowed_domain(self, monkeypatch):
        _patch_google_verify(
            monkeypatch, {"email": "jane@lvairo.com", "email_verified": True}
        )
        verifier = WorkspaceTokenVerifier(allowed_domains=["lvairo.com"])
        assert await verifier.verify_token("tok") is not None

    async def test_rejects_other_domain(self, monkeypatch):
        _patch_google_verify(
            monkeypatch, {"email": "mallory@gmail.com", "email_verified": True}
        )
        verifier = WorkspaceTokenVerifier(allowed_domains=["lvairo.com"])
        assert await verifier.verify_token("tok") is None

    async def test_rejects_lookalike_domain(self, monkeypatch):
        _patch_google_verify(
            monkeypatch, {"email": "x@lvairo.com.evil.io", "email_verified": True}
        )
        verifier = WorkspaceTokenVerifier(allowed_domains=["lvairo.com"])
        assert await verifier.verify_token("tok") is None

    async def test_rejects_unverified_email(self, monkeypatch):
        _patch_google_verify(
            monkeypatch, {"email": "jane@lvairo.com", "email_verified": False}
        )
        verifier = WorkspaceTokenVerifier(allowed_domains=["lvairo.com"])
        assert await verifier.verify_token("tok") is None

    async def test_accepts_string_true_verified(self, monkeypatch):
        # Google's tokeninfo endpoint returns email_verified as a string.
        _patch_google_verify(
            monkeypatch, {"email": "jane@lvairo.com", "email_verified": "true"}
        )
        verifier = WorkspaceTokenVerifier(allowed_domains=["lvairo.com"])
        assert await verifier.verify_token("tok") is not None

    async def test_rejects_missing_email(self, monkeypatch):
        _patch_google_verify(monkeypatch, {"email_verified": True})
        verifier = WorkspaceTokenVerifier(allowed_domains=["lvairo.com"])
        assert await verifier.verify_token("tok") is None

    async def test_upstream_rejection_passes_through(self, monkeypatch):
        _patch_google_verify(monkeypatch, None)
        verifier = WorkspaceTokenVerifier(allowed_domains=["lvairo.com"])
        assert await verifier.verify_token("tok") is None

    async def test_domain_comparison_is_case_insensitive(self, monkeypatch):
        _patch_google_verify(
            monkeypatch, {"email": "Jane@LVAIRO.com", "email_verified": True}
        )
        verifier = WorkspaceTokenVerifier(allowed_domains=["@Lvairo.COM "])
        assert await verifier.verify_token("tok") is not None

    def test_requires_at_least_one_domain(self):
        with pytest.raises(AuthConfigError):
            WorkspaceTokenVerifier(allowed_domains=["", "  "])


# ---------------------------------------------------------------------------
# GoogleWorkspaceProvider
# ---------------------------------------------------------------------------


def make_provider(**kwargs) -> GoogleWorkspaceProvider:
    defaults = {
        "client_id": "cid.apps.googleusercontent.com",
        "client_secret": "sec",
        "base_url": "https://mcap.example.com",
        "allowed_domains": ["lvairo.com"],
    }
    defaults.update(kwargs)
    return GoogleWorkspaceProvider(**defaults)


class TestGoogleWorkspaceProvider:
    def test_installs_workspace_verifier(self):
        # If fastmcp renames _token_validator, the domain check would silently
        # disappear — this asserts the swap actually landed.
        provider = make_provider()
        assert isinstance(provider._token_validator, WorkspaceTokenVerifier)
        assert provider.allowed_domains == {"lvairo.com"}

    def test_single_domain_preselects_workspace_account_picker(self):
        provider = make_provider()
        assert provider._extra_authorize_params.get("hd") == "lvairo.com"

    def test_multiple_domains_skip_hd_hint(self):
        provider = make_provider(allowed_domains=["lvairo.com", "pixel-robotics.eu"])
        assert "hd" not in (provider._extra_authorize_params or {})
        assert provider.allowed_domains == {"lvairo.com", "pixel-robotics.eu"}

    def test_email_scope_is_required(self):
        provider = make_provider()
        scopes = provider._token_validator.required_scopes
        assert "https://www.googleapis.com/auth/userinfo.email" in scopes

    def test_requires_a_domain(self):
        with pytest.raises(AuthConfigError):
            make_provider(allowed_domains=[])


# ---------------------------------------------------------------------------
# build_auth
# ---------------------------------------------------------------------------


class TestBuildAuth:
    def test_stdio_needs_no_auth(self):
        assert build_auth(ServerConfig()) is None

    def test_http_without_auth_refuses_to_start(self):
        with pytest.raises(AuthConfigError, match="Refusing to serve"):
            build_auth(ServerConfig(transport="http"))

    def test_http_insecure_optout(self):
        config = ServerConfig(transport="http", insecure_no_auth=True)
        assert build_auth(config) is None

    def test_incomplete_google_config_is_rejected(self):
        config = ServerConfig(transport="http", google_client_id="cid")
        with pytest.raises(AuthConfigError, match="missing"):
            build_auth(config)

    def test_full_config_builds_provider(self):
        config = ServerConfig(
            transport="http",
            google_client_id="cid.apps.googleusercontent.com",
            google_client_secret="sec",
            base_url="https://mcap.example.com",
            allowed_google_domains=["lvairo.com"],
        )
        provider = build_auth(config)
        assert isinstance(provider, GoogleWorkspaceProvider)
        assert provider.allowed_domains == {"lvairo.com"}

    def test_create_server_wires_auth(self, tmp_path):
        from mcap_mcp_server.server import create_server

        config = ServerConfig(
            data_dir=tmp_path,
            transport="http",
            google_client_id="cid.apps.googleusercontent.com",
            google_client_secret="sec",
            base_url="https://mcap.example.com",
            allowed_google_domains=["lvairo.com"],
        )
        server = create_server(config)
        assert isinstance(server.auth, GoogleWorkspaceProvider)

    def test_create_server_refuses_unauthenticated_http(self, tmp_path):
        from mcap_mcp_server.server import create_server

        with pytest.raises(AuthConfigError):
            create_server(ServerConfig(data_dir=tmp_path, transport="http"))
