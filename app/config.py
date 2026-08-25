"""Application configuration.

Everything environment-specific is read from `.env` (or the real process
environment) exactly once, at import time, through a single Settings object.
Nothing in the codebase is allowed to call os.getenv() directly — that is how
you end up with a secret read from two different places with two different
defaults.
"""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Unknown keys in .env are ignored rather than fatal, so adding an
        # unrelated variable to your shell cannot stop the app from booting.
        extra="ignore",
    )

    # --- Database ------------------------------------------------------------
    database_url: str
    test_database_url: str | None = None

    # --- Stripe (used from Phase 4 onward) -----------------------------------
    stripe_api_key: str | None = None
    stripe_webhook_secret: str | None = None

    @field_validator("stripe_api_key")
    @classmethod
    def _must_be_a_test_key(cls, value: str | None) -> str | None:
        """Refuse to start with a live Stripe key.

        WHY: this project has no authorisation layer, no fraud controls and a
        webhook handler that will happily approve card spend. Pointing it at a
        live key would move real money. A live key is therefore treated as a
        configuration bug and killed at boot, rather than trusted to be caught
        in review. The placeholder in .env.example is allowed through so the
        app still starts before you have signed up for Stripe.
        """
        if value is None or value == "sk_test_replace_me":
            return value
        if not value.startswith("sk_test_"):
            raise ValueError(
                "STRIPE_API_KEY must be a test-mode key (sk_test_...). "
                "MiniLedger refuses to run against live Stripe credentials."
            )
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor, so the .env file is parsed once per process."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
