"""Azure OpenAI Connection Profile Data Model."""

from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class ConnectionProfile:
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = "Default Azure Profile"
    endpoint: str = ""
    api_key: str = ""
    deployment: str = ""
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    long_context_threshold: int | None = 128_000
    long_input_price_per_million: float | None = None
    long_output_price_per_million: float | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.endpoint.strip() and self.api_key.strip() and self.deployment.strip())

    def to_dict(self, include_sensitive: bool = False) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "endpoint": self.endpoint,
            "deployment": self.deployment,
            "input_price_per_million": self.input_price_per_million,
            "output_price_per_million": self.output_price_per_million,
            "long_context_threshold": self.long_context_threshold,
            "long_input_price_per_million": self.long_input_price_per_million,
            "long_output_price_per_million": self.long_output_price_per_million,
            "is_configured": self.is_configured,
            "has_api_key": bool(self.api_key.strip()),
        }
        if include_sensitive:
            data["api_key"] = self.api_key
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ConnectionProfile":
        input_price = data.get("input_price_per_million")
        output_price = data.get("output_price_per_million")
        threshold = data.get("long_context_threshold")
        long_in = data.get("long_input_price_per_million")
        long_out = data.get("long_output_price_per_million")
        return cls(
            id=str(data.get("id") or uuid4()),
            name=str(data.get("name") or "Azure Profile").strip(),
            endpoint=str(data.get("endpoint") or "").strip(),
            api_key=str(data.get("api_key") or "").strip(),
            deployment=str(data.get("deployment") or "").strip(),
            input_price_per_million=float(input_price) if input_price is not None and str(input_price).strip() != "" else None,
            output_price_per_million=float(output_price) if output_price is not None and str(output_price).strip() != "" else None,
            long_context_threshold=int(threshold) if threshold is not None and str(threshold).strip() != "" else 128_000,
            long_input_price_per_million=float(long_in) if long_in is not None and str(long_in).strip() != "" else None,
            long_output_price_per_million=float(long_out) if long_out is not None and str(long_out).strip() != "" else None,
        )
