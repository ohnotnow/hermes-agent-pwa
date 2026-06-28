"""Runtime configuration for the hap gateway (env-driven, minimal).

YAGNI: plain os.getenv, no settings library. The auth token is the single
shared bearer token (human login + agent auth); see ant hap-VYQvH.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

VERSION = "0.1.0"


@dataclass(frozen=True)
class Settings:
    db_path: str
    auth_token: str
    host: str
    port: int
    cookie_secure: bool


def _load_token() -> str:
    """Bearer token from env, or a token file (lets us launch without setting
    shell env vars). HAP_TOKEN_FILE overrides the default path."""
    token = os.getenv("HAP_AUTH_TOKEN", "")
    if token:
        return token
    token_file = os.getenv("HAP_TOKEN_FILE", "hap_token.txt")
    try:
        with open(token_file, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def load_settings() -> Settings:
    return Settings(
        db_path=os.getenv("HAP_DB_PATH", "hap.db"),
        auth_token=_load_token(),
        host=os.getenv("HAP_HOST", "127.0.0.1"),
        port=int(os.getenv("HAP_PORT", "8088")),
        # Production (behind Caddy/HTTPS) must set HAP_COOKIE_SECURE=true.
        # Default false so local http dev/testing works.
        cookie_secure=os.getenv("HAP_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"},
    )
