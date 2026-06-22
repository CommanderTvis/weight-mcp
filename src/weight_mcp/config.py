"""Configuration for the single self-hosting user.

Everything is read from the environment (``.env`` in local/dev). There is no
per-user config: this server is designed for exactly one person who also hosts it.
"""

from __future__ import annotations

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
    # The single shared secret. The OAuth gate asks for nothing but this.
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

    # --- goals --------------------------------------------------------------
    # The author is an under-eater: the default goal is a *floor* (eat at least
    # this much). Set goal_mode="ceiling" for weight-loss / deficit framing.
    goal_mode: GoalMode = "floor"
    calorie_target_kcal: int = 2600
    protein_target_g: int = 150

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
