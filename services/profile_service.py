"""Profile management service with JSON file persistence."""

import json
import logging
from pathlib import Path
from uuid import uuid4

from models.profile import ConnectionProfile

logger = logging.getLogger(__name__)


class ProfileService:
    def __init__(self, storage_path: Path) -> None:
        self.storage_path = storage_path
        self._profiles: dict[str, ConnectionProfile] = {}
        self._active_profile_id: str | None = None
        self._load()

    def _load(self) -> None:
        if not self.storage_path.exists():
            return
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            profiles_data = data.get("profiles", [])
            for pdata in profiles_data:
                profile = ConnectionProfile.from_dict(pdata)
                self._profiles[profile.id] = profile
            self._active_profile_id = data.get("active_profile_id")
            if self._profiles and (not self._active_profile_id or self._active_profile_id not in self._profiles):
                self._active_profile_id = next(iter(self._profiles.keys()))
        except (OSError, json.JSONDecodeError, ValueError):
            logger.exception("Could not load profiles.json, initializing fresh storage.")

    def _save(self) -> None:
        payload = {
            "active_profile_id": self._active_profile_id,
            "profiles": [p.to_dict(include_sensitive=True) for p in self._profiles.values()],
        }
        try:
            temporary = self.storage_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.storage_path)
        except OSError:
            logger.exception("Failed to write profiles to %s", self.storage_path)

    def list_profiles(self) -> list[dict]:
        return [p.to_dict(include_sensitive=False) for p in self._profiles.values()]

    def get_active_profile(self) -> ConnectionProfile | None:
        if self._active_profile_id and self._active_profile_id in self._profiles:
            return self._profiles[self._active_profile_id]
        if self._profiles:
            self._active_profile_id = next(iter(self._profiles.keys()))
            self._save()
            return self._profiles[self._active_profile_id]
        return None

    def get_profile(self, profile_id: str) -> ConnectionProfile | None:
        return self._profiles.get(profile_id)

    def set_active_profile(self, profile_id: str) -> ConnectionProfile:
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
        long_context_threshold: int | None = 128_000,
        long_input_price_per_million: float | None = None,
        long_output_price_per_million: float | None = None,
    ) -> ConnectionProfile:
        cleaned_name = name.strip() or "New Azure Profile"
        profile = ConnectionProfile(
            id=str(uuid4()),
            name=cleaned_name,
            endpoint=endpoint.strip(),
            api_key=api_key.strip(),
            deployment=deployment.strip(),
            input_price_per_million=input_price_per_million,
            output_price_per_million=output_price_per_million,
            long_context_threshold=long_context_threshold if long_context_threshold is not None else 128_000,
            long_input_price_per_million=long_input_price_per_million,
            long_output_price_per_million=long_output_price_per_million,
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
    ) -> ConnectionProfile:
        profile = self.get_profile(profile_id)
        if not profile:
            raise ValueError("Profile not found.")
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
            profile.long_output_price_per_million = long_output_price_per_million

        self._save()
        return profile

    def delete_profile(self, profile_id: str) -> bool:
        if profile_id not in self._profiles:
            return False
        del self._profiles[profile_id]
        if self._active_profile_id == profile_id:
            self._active_profile_id = next(iter(self._profiles.keys())) if self._profiles else None
        self._save()
        return True

    def get_summary(self) -> dict:
        active = self.get_active_profile()
        return {
            "active_profile_id": self._active_profile_id,
            "active_profile": active.to_dict(include_sensitive=False) if active else None,
            "profiles": self.list_profiles(),
        }
