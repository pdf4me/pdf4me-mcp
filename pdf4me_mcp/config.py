import os
from dataclasses import dataclass, field


@dataclass
class Config:
    api_key: str | None = field(default_factory=lambda: os.getenv(
        "PDF4ME_API_KEY", None))
    pdf4me_base_url: str = field(default_factory=lambda: os.getenv(
        "PDF4ME_BASE_URL", "https://api.pdf4me.com"))


config = Config()
