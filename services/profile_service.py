"""Manage Azure OpenAI connection profiles and persist the active selection as local JSON."""

import json
import logging
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from models.input_budget import normalize_input_budget_settings
from models.profile import (
    CONTEXT_FIELDS, DEFAULT_LONG_CONTEXT_THRESHOLD, ConnectionProfile,
    normalize_output_limit_settings,
)

logger = logging.getLogger(__name__)


class _UnsetOutputLimit:
    """Distinguish omitted output-cap keywords from explicitly cleared values."""


_UNSET_OUTPUT_LIMIT = _UnsetOutputLimit()


class ProfileNotFoundError(ValueError):
    """Distinguish absent profiles from invalid policy updates."""


class ProfileService:
    """Manage stored connection profiles and the active profile selection."""

    def __init__(self, storage_path: Path) -> None:
        """Initialize the profile registry and load the configured storage file."""
        self.storage_path = storage_path
        self._profiles: dict[str, ConnectionProfile] = {}
        self._active_profile_id: str | None = None
        self._load()

    def _load(self) -> None:
        """Load profiles and recover a valid active selection when the stored one is absent."""
        if not self.storage_path.exists():
            return
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            profiles_data = data.get("profiles", [])
            for pdata in profiles_data:
                profile = ConnectionProfile.from_dict(pdata)
                self._profiles[profile.id] = profile
            self._active_profile_id = data.get("active_profile_id")
            if self._profiles and (
                not self._active_profile_id
                or self._active_profile_id not in self._profiles
            ):
                self._active_profile_id = next(iter(self._profiles.keys()))
        except (OSError, json.JSONDecodeError, ValueError):
            logger.exception(
                "Could not load profiles.json, initializing fresh storage."
            )

    def _save(self) -> None:
        """Write profiles through a temporary file, logging filesystem failures."""
        payload = {
            "active_profile_id": self._active_profile_id,
            "profiles": [
                p.to_dict(include_sensitive=True)
                for p in self._profiles.values()
            ],
        }
        try:
            temporary = self.storage_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.storage_path)
        except OSError:
            logger.exception(
                "Failed to write profiles to %s", self.storage_path
            )

    def list_profiles(self) -> list[dict]:
        """Return all profiles with their API keys excluded."""
        return [
            p.to_dict(include_sensitive=False, include_legacy_output_limit=True)
            for p in self._profiles.values()
        ]

    def get_active_profile(self) -> ConnectionProfile | None:
        """Return the active profile, persisting a fallback selection when necessary."""
        if (
            self._active_profile_id
            and self._active_profile_id in self._profiles
        ):
            return self._profiles[self._active_profile_id]
        if self._profiles:
            self._active_profile_id = next(iter(self._profiles.keys()))
            self._save()
            return self._profiles[self._active_profile_id]
        return None

    def get_profile(self, profile_id: str) -> ConnectionProfile | None:
        """Return a profile by identifier, or None when absent."""
        return self._profiles.get(profile_id)

    def set_active_profile(self, profile_id: str) -> ConnectionProfile:
        """Select and persist an existing profile, rejecting an unknown identifier."""
        if profile_id not in self._profiles:
            raise ValueError("Profile not found.")
        self._active_profile_id = profile_id
        self._save()
        return self._profiles[profile_id]

    def create_profile(
        self,
        name: str,
        endpoint: str,
        api_key: str,
        deployment: str,
        input_price_per_million: float | None = None,
        output_price_per_million: float | None = None,
        long_context_threshold: int | None = DEFAULT_LONG_CONTEXT_THRESHOLD,
        long_input_price_per_million: float | None = None,
        long_output_price_per_million: float | None = None,
        cache_read_price_per_million: float | None = None,
        cache_write_price_per_million: float | None = None,
        long_cache_read_price_per_million: float | None = None,
        long_cache_write_price_per_million: float | None = None,
        context_preflight_mode: str | None = None,
        context_input_budget: int | None = None,
        model_context_window: int | None = None,
        model_max_input_tokens: int | None = None,
        model_max_output_tokens: int | None | _UnsetOutputLimit = (
            _UNSET_OUTPUT_LIMIT
        ),
        *,
        output_token_limit: int | None | _UnsetOutputLimit = _UNSET_OUTPUT_LIMIT,
        declared_model_max_output_tokens: int | None = None,
        context_input_budget_mode: str | None = None,
    ) -> ConnectionProfile:
        """Create a normalized profile and select it when no active profile exists."""
        cleaned_name = name.strip() or "New Azure Profile"
        output_settings = {}
        if model_max_output_tokens is not _UNSET_OUTPUT_LIMIT:
            output_settings["model_max_output_tokens"] = model_max_output_tokens
        if output_token_limit is not _UNSET_OUTPUT_LIMIT:
            output_settings["output_token_limit"] = output_token_limit
        output_settings = normalize_output_limit_settings(output_settings)
        profile = ConnectionProfile(
            id=str(uuid4()),
            name=cleaned_name,
            endpoint=endpoint.strip(),
            api_key=api_key.strip(),
            deployment=deployment.strip(),
            input_price_per_million=input_price_per_million,
            output_price_per_million=output_price_per_million,
            long_context_threshold=(
                long_context_threshold
                if long_context_threshold is not None
                else DEFAULT_LONG_CONTEXT_THRESHOLD
            ),
            long_input_price_per_million=long_input_price_per_million,
            long_output_price_per_million=long_output_price_per_million,
            cache_read_price_per_million=cache_read_price_per_million,
            cache_write_price_per_million=cache_write_price_per_million,
            long_cache_read_price_per_million=long_cache_read_price_per_million,
            long_cache_write_price_per_million=long_cache_write_price_per_million,
            context_preflight_mode=context_preflight_mode,
            context_input_budget=context_input_budget,
            context_input_budget_mode=context_input_budget_mode,
            model_context_window=model_context_window,
            model_max_input_tokens=model_max_input_tokens,
            output_token_limit=output_settings.get("output_token_limit"),
            declared_model_max_output_tokens=declared_model_max_output_tokens,
        )
        self._profiles[profile.id] = profile
        if not self._active_profile_id:
            self._active_profile_id = profile.id
        self._save()
        return profile

    def update_profile(
        self,
        profile_id: str,
        name: str | None = None,
        endpoint: str | None = None,
        api_key: str | None = None,
        deployment: str | None = None,
        input_price_per_million: float | None = None,
        output_price_per_million: float | None = None,
        clear_input_price: bool = False,
        clear_output_price: bool = False,
        long_context_threshold: int | None = None,
        long_input_price_per_million: float | None = None,
        long_output_price_per_million: float | None = None,
        clear_long_input_price: bool = False,
        clear_long_output_price: bool = False,
        cache_read_price_per_million: float | None = None,
        cache_write_price_per_million: float | None = None,
        long_cache_read_price_per_million: float | None = None,
        long_cache_write_price_per_million: float | None = None,
        clear_cache_read_price: bool = False,
        clear_cache_write_price: bool = False,
        clear_long_cache_read_price: bool = False,
        clear_long_cache_write_price: bool = False,
        context_settings: dict | None = None,
    ) -> ConnectionProfile:
        """Apply provided profile values and pricing-clear flags, preserving omitted settings."""
        profile = self.get_profile(profile_id)
        if not profile:
            raise ProfileNotFoundError("Profile not found.")
        changes = normalize_input_budget_settings(
            normalize_output_limit_settings(context_settings or {})
        )
        if set(changes) - set(CONTEXT_FIELDS):
            raise ValueError("Unknown context setting.")
        validated = replace(profile, **changes)
        for field_name in changes:
            setattr(profile, field_name, getattr(validated, field_name))
        if name is not None and name.strip():
            profile.name = name.strip()
        if endpoint is not None:
            profile.endpoint = endpoint.strip()
        if api_key is not None and api_key.strip():
            profile.api_key = api_key.strip()
        if deployment is not None and deployment.strip():
            profile.deployment = deployment.strip()
        if clear_input_price:
            profile.input_price_per_million = None
        elif input_price_per_million is not None:
            profile.input_price_per_million = input_price_per_million
        if clear_output_price:
            profile.output_price_per_million = None
        elif output_price_per_million is not None:
            profile.output_price_per_million = output_price_per_million
        if long_context_threshold is not None:
            profile.long_context_threshold = long_context_threshold
        if clear_long_input_price:
            profile.long_input_price_per_million = None
        elif long_input_price_per_million is not None:
            profile.long_input_price_per_million = long_input_price_per_million
        if clear_long_output_price:
            profile.long_output_price_per_million = None
        elif long_output_price_per_million is not None:
            profile.long_output_price_per_million = (
                long_output_price_per_million
            )
        for name, value, clear in (
            ("cache_read_price_per_million", cache_read_price_per_million, clear_cache_read_price),
            ("cache_write_price_per_million", cache_write_price_per_million, clear_cache_write_price),
            ("long_cache_read_price_per_million", long_cache_read_price_per_million, clear_long_cache_read_price),
            ("long_cache_write_price_per_million", long_cache_write_price_per_million, clear_long_cache_write_price),
        ):
            if clear:
                setattr(profile, name, None)
            elif value is not None:
                setattr(profile, name, value)

        self._save()
        return profile

    def delete_profile(self, profile_id: str) -> bool:
        """Remove a profile and choose a replacement active profile when needed."""
        if profile_id not in self._profiles:
            return False
        del self._profiles[profile_id]
        if self._active_profile_id == profile_id:
            self._active_profile_id = (
                next(iter(self._profiles.keys())) if self._profiles else None
            )
        self._save()
        return True

    def get_summary(self) -> dict:
        """Return the active selection and profile list without exposing API keys."""
        active = self.get_active_profile()
        return {
            "active_profile_id": self._active_profile_id,
            "active_profile": (
                active.to_dict(
                    include_sensitive=False,
                    include_legacy_output_limit=True,
                ) if active else None
            ),
            "profiles": self.list_profiles(),
        }
