"""Manage Azure OpenAI connection profiles and persist the active selection as local JSON."""

import hashlib
import math
from dataclasses import fields, replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from models.app_settings import AppSettings, DEFAULT_APP_SETTINGS
from models.input_budget import normalize_input_budget_settings
from models.profile import (
    PROFILE_POLICY_FIELDS, DEFAULT_LONG_CONTEXT_THRESHOLD, ConnectionProfile,
    LegacyInputBoundary, normalize_output_limit_settings,
)
from services.context_budget import ContextBudget
from services.context_policy import ContextPolicy
from services.json_storage import JsonDocumentStore, JsonStorageError


class _UnsetOutputLimit:
    """Distinguish omitted output-cap keywords from explicitly cleared values."""


_UNSET_OUTPUT_LIMIT = _UnsetOutputLimit()


class ProfileNotFoundError(ValueError):
    """Distinguish absent profiles from invalid policy updates."""


class ProfileStorageError(JsonStorageError):
    """Reject unsafe reads or failed profile writes without discarding connection data."""


class ProfileService:
    """Manage stored connection profiles and the active profile selection."""

    def __init__(
        self, storage_path: Path, *, app_settings: AppSettings | None = None,
    ) -> None:
        """Load a complete validated registry without writing a migration during startup."""
        self.storage_path = storage_path
        self._profiles: dict[str, ConnectionProfile] = {}
        self._active_profile_id: str | None = None
        self._app_settings = app_settings or replace(
            DEFAULT_APP_SETTINGS, context_input_budget_mode="legacy",
        )
        self._app_settings_source = (
            "saved_settings" if app_settings is not None else "historical_defaults"
        )
        self._migration_policy: dict | None = None
        self._load()

    def _load(self) -> None:
        """Validate all profiles before publishing any, preserving invalid files for repair."""
        try:
            self._document = JsonDocumentStore(self.storage_path, "profiles")
            data = self._document.payload
            if data is None:
                return
            legacy = self._document.version == 0
            list_key = "profiles" if legacy else "items"
            if set(data) - {"active_profile_id", list_key} or list_key not in data:
                raise ValueError("Invalid profile payload.")
            if not legacy and set(data) != {"active_profile_id", "items"}:
                raise ValueError("Invalid versioned profile payload.")
            profiles_data = data[list_key]
            if not isinstance(profiles_data, list):
                raise ValueError("Profiles must be a list.")
            active_id = data.get("active_profile_id")
            if active_id is not None and not isinstance(active_id, str):
                raise ValueError("Invalid active profile identifier.")
            profiles = {}
            digest = hashlib.sha256(self._document.original).hexdigest()
            for index, raw in enumerate(profiles_data):
                self._validate_profile_data(raw, legacy=legacy)
                pdata = dict(raw)
                if legacy and not pdata.get("id"):
                    pdata["id"] = str(uuid5(
                        NAMESPACE_URL, f"helios:profiles:{digest}:{index}",
                    ))
                profile = (
                    ConnectionProfile.from_dict(pdata) if legacy
                    else ConnectionProfile(**{
                        name: value for name, value in pdata.items()
                        if name not in {"is_configured", "has_api_key"}
                    })
                )
                if profile.id in profiles:
                    raise ValueError("Duplicate profile identifier.")
                profiles[profile.id] = profile
            recovered = bool(profiles and active_id not in profiles)
            if not legacy and (recovered or not profiles and active_id is not None):
                raise ValueError("Active profile is not present.")
            if legacy:
                if recovered:
                    active_id = next(iter(profiles))
                elif not profiles:
                    active_id = None
                self._migration_policy = self._capture_legacy_policy(
                    profiles, recovered,
                )
            elif self._document.migration is not None:
                self._migration_policy = self._document.migration["policy"]
                self._restore_legacy_policy(profiles, self._migration_policy)
            self._profiles, self._active_profile_id = profiles, active_id
        except JsonStorageError as exc:
            raise ProfileStorageError(str(exc), reason=exc.reason) from None
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise ProfileStorageError("Connection profiles are invalid; repair profiles.json before starting. The saved file has not been changed.", reason="invalid_document") from None

    @staticmethod
    def _validate_profile_data(data: dict, *, legacy: bool) -> None:
        """Reject malformed or unknown stored fields before normalizing legacy values."""
        if not isinstance(data, dict):
            raise ValueError("Profile must be an object.")
        known = {
            field.name for field in fields(ConnectionProfile)
        } - {"legacy_input_boundary"}
        known |= {"is_configured", "has_api_key"}
        allowed = known | ({"model_max_output_tokens"} if legacy else set())
        if set(data) - allowed or not legacy and set(data) != known:
            raise ValueError("Profile fields do not match their version.")
        for name in ("id", "name", "endpoint", "api_key", "deployment"):
            value = data.get(name)
            if value is not None and not isinstance(value, str):
                raise ValueError("Connection fields must be strings.")
            if not legacy and value is None:
                raise ValueError("Connection fields must be strings.")
        for name in known:
            if not name.endswith("price_per_million"):
                continue
            value = data.get(name)
            if value is None or legacy and isinstance(value, str) and not value.strip():
                continue
            if type(value) not in {int, float} and not (
                legacy and isinstance(value, str)
            ):
                raise ValueError("Invalid stored price.")
            if not math.isfinite(float(value)) or float(value) < 0:
                raise ValueError("Invalid stored price.")
        threshold = data.get("long_context_threshold")
        if threshold is not None and threshold != "":
            if type(threshold) is bool or not legacy and type(threshold) is not int:
                raise ValueError("Invalid price threshold.")
            if int(threshold) < 1 or float(threshold) != int(threshold):
                raise ValueError("Invalid price threshold.")
        if not legacy:
            if data["context_input_budget_mode"] is None:
                raise ValueError("A versioned input policy must be explicit.")
            if any(
                type(data[name]) is not bool
                for name in ("is_configured", "has_api_key")
            ):
                raise ValueError("Invalid derived profile flags.")

    def _capture_legacy_policy(self, profiles: dict, recovered: bool) -> dict:
        """Keep original limits separate from editable declarations and user choices."""
        settings = self._app_settings
        budget = ContextBudget(
            settings.context_token_budget, settings.context_output_reserve,
            settings.max_summary_calls, reserve_output=False,
        )
        boundaries = {}
        app_policy = ContextPolicy.resolve(
            ConnectionProfile(), budget, settings.context_preflight_mode,
            default_input_budget_mode=settings.context_input_budget_mode,
            default_input_budget=settings.context_input_budget,
        )
        for profile in profiles.values():
            if profile.context_input_budget_mode != "legacy":
                continue
            policy = ContextPolicy.resolve(
                profile, budget, settings.context_preflight_mode,
                default_input_budget_mode=settings.context_input_budget_mode,
                default_input_budget=settings.context_input_budget,
            )
            boundary = LegacyInputBoundary(
                policy.input_budget, policy.declared_model_input_limit,
                settings.context_output_reserve, profile.output_token_limit,
                policy.max_output_tokens,
                app_policy.input_budget, profile.context_input_budget,
            )
            profile.legacy_input_boundary = boundary
            boundaries[profile.id] = boundary.to_dict()
        return {
            "app_settings_source": self._app_settings_source,
            "app_settings": settings.to_dict(),
            "legacy_input_boundaries": boundaries,
            "active_selection_recovered": recovered,
        }

    @staticmethod
    def _restore_legacy_policy(profiles: dict, policy: dict) -> None:
        """Validate historical policy evidence before attaching immutable input boundaries."""
        if set(policy) != {
            "app_settings_source", "app_settings", "legacy_input_boundaries",
            "active_selection_recovered",
        } or policy["app_settings_source"] not in {
            "saved_settings", "historical_defaults",
        }:
            raise ValueError("Invalid profile migration policy.")
        if set(policy["app_settings"]) != set(DEFAULT_APP_SETTINGS.to_dict()):
            raise ValueError("Invalid application policy evidence.")
        AppSettings(**policy["app_settings"])
        if (
            type(policy["active_selection_recovered"]) is not bool
            or not isinstance(policy["legacy_input_boundaries"], dict)
        ):
            raise ValueError("Invalid profile migration policy.")
        for profile_id, raw in policy["legacy_input_boundaries"].items():
            if not isinstance(profile_id, str) or not profile_id.strip():
                raise ValueError("Invalid historical profile identifier.")
            boundary = LegacyInputBoundary(**raw)
            if profile_id in profiles:
                profiles[profile_id].legacy_input_boundary = boundary

    def _save(
        self, profiles: dict | None = None, active_profile_id: str | None = None,
    ) -> None:
        """Persist a candidate registry without publishing any in-memory mutation."""
        if profiles is None:
            profiles, active_profile_id = self._profiles, self._active_profile_id
        payload = {
            "active_profile_id": active_profile_id,
            "items": [
                p.to_dict(include_sensitive=True)
                for p in profiles.values()
            ],
        }
        try:
            self._document.save(payload, migration_policy=self._migration_policy)
        except JsonStorageError as exc:
            raise ProfileStorageError(str(exc), reason=exc.reason) from None

    def list_profiles(self) -> list[dict]:
        """Return all profiles with their API keys excluded."""
        return [
            p.to_dict(include_sensitive=False, include_legacy_output_limit=True)
            for p in self._profiles.values()
        ]

    def get_active_profile(self) -> ConnectionProfile | None:
        """Return the active profile without rewriting storage during a read."""
        if (
            self._active_profile_id
            and self._active_profile_id in self._profiles
        ):
            return self._profiles[self._active_profile_id]
        if self._profiles:
            return next(iter(self._profiles.values()))
        return None

    def get_profile(self, profile_id: str) -> ConnectionProfile | None:
        """Return a profile by identifier, or None when absent."""
        return self._profiles.get(profile_id)

    def set_active_profile(self, profile_id: str) -> ConnectionProfile:
        """Select and persist an existing profile, rejecting an unknown identifier."""
        if profile_id not in self._profiles:
            raise ProfileNotFoundError("Profile not found.")
        self._save(self._profiles, profile_id)
        self._active_profile_id = profile_id
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
        memory_enabled: bool | None = False,
        memory_output_headroom: int | None = None,
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
            memory_enabled=memory_enabled,
            memory_output_headroom=memory_output_headroom,
            model_context_window=model_context_window,
            model_max_input_tokens=model_max_input_tokens,
            output_token_limit=output_settings.get("output_token_limit"),
            declared_model_max_output_tokens=declared_model_max_output_tokens,
        )
        profiles = {**self._profiles, profile.id: profile}
        active_id = self._active_profile_id or profile.id
        self._save(profiles, active_id)
        self._profiles, self._active_profile_id = profiles, active_id
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
        original = self.get_profile(profile_id)
        if not original:
            raise ProfileNotFoundError("Profile not found.")
        changes = normalize_input_budget_settings(
            normalize_output_limit_settings(context_settings or {})
        )
        if set(changes) - set(PROFILE_POLICY_FIELDS):
            raise ValueError("Unknown context setting.")
        profile = replace(original, **changes)
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

        profile = replace(profile)
        self._save({**self._profiles, profile_id: profile}, self._active_profile_id)
        for field in fields(ConnectionProfile):
            setattr(original, field.name, getattr(profile, field.name))
        return original

    def delete_profile(self, profile_id: str) -> bool:
        """Remove a profile and choose a replacement active profile when needed."""
        if profile_id not in self._profiles:
            return False
        profiles = {
            key: profile for key, profile in self._profiles.items()
            if key != profile_id
        }
        active_id = self._active_profile_id
        if active_id == profile_id:
            active_id = next(iter(profiles), None)
        self._save(profiles, active_id)
        self._profiles, self._active_profile_id = profiles, active_id
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
