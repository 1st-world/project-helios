"""Azure OpenAI Connection Profile Data Model."""

from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class ConnectionProfile:
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = "Default Azure Profile"
    endpoint: str = ""
    api_version: str = "2025-04-01-preview"
    api_key: str = ""
    deployment: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.endpoint.strip() and self.api_version.strip() and self.api_key.strip() and self.deployment.strip())

    def to_dict(self, include_sensitive: bool = False) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "endpoint": self.endpoint,
            "api_version": self.api_version,
            "deployment": self.deployment,
            "is_configured": self.is_configured,
            "has_api_key": bool(self.api_key.strip()),
        }
        if include_sensitive:
            data["api_key"] = self.api_key
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ConnectionProfile":
        return cls(
            id=str(data.get("id") or uuid4()),
            name=str(data.get("name") or "Azure Profile").strip(),
            endpoint=str(data.get("endpoint") or "").strip(),
            api_version=str(data.get("api_version") or "2025-04-01-preview").strip(),
            api_key=str(data.get("api_key") or "").strip(),
            deployment=str(data.get("deployment") or "").strip(),
        )
