"""Configuration for the self-hosting admin.

Everything is read from the environment (``.env`` in local/dev). The ``.env``
holds exactly one account: the admin's. Non-admin accounts are not configured
here — the admin registers them at runtime via MCP tools and they live in the
database.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

GoalMode = Literal["floor", "ceiling"]


class Settings(BaseSettings):
    """Server settings, prefixed ``WEIGHT_MCP_`` in the environment."""

    model_config = SettingsConfigDict(
        env_prefix="WEIGHT_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- auth ---------------------------------------------------------------
    # The admin account's password (username: "admin"). Also the JWT signing
    # root: rotating it invalidates every outstanding token for every user.
    password: str = Field(min_length=1)

    # Public, externally reachable base URL of this server, e.g.
    # "https://weight.example.com". Required so the OAuth metadata documents
    # absolute endpoint URLs that claude.ai can reach.
    public_base_url: str = "http://localhost:8000"

    # --- network ------------------------------------------------------------
    host: str = "0.0.0.0"  # noqa: S104 - self-hosted, bind all interfaces by default
    port: int = 8000

    # --- storage ------------------------------------------------------------
    database_path: Path = Path("data/weight.sqlite3")

    # --- locale -------------------------------------------------------------
    # Default region/language for nutrition lookups: a user in Germany.
    region: str = "DE"
    language: str = "de"

    # --- nutrition sources --------------------------------------------------
    # Open Food Facts is the primary source and needs no key. It is filtered to
    # German products by default for better relevance.
    off_enabled: bool = True
    off_country: str = "germany"

    # USDA FoodData Central is an optional secondary source (generic foods);
    # it requires a free API key, so it is off unless a key is provided.
    usda_enabled: bool = False
    usda_api_key: str | None = None

    @property
    def issuer(self) -> str:
        """OAuth issuer / resource identifier (the public base URL, no slash)."""
        return self.public_base_url.rstrip("/")


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
