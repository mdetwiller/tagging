from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel

BACKEND_DIR = Path(__file__).resolve().parent.parent
TAGGING_DIR = BACKEND_DIR.parent

# Looked up in order; the first file holding the variable wins.
TYPESAFE_ENV_FILES: tuple[Path, ...] = (
    TAGGING_DIR / ".env",
    TAGGING_DIR.parent / "Codestormer" / ".env",
)
OPENAI_ENV_FILE = TAGGING_DIR.parent / "backend" / ".env"


def read_env_var(name: str, env_file: Path) -> str | None:
    """The environment variable if set, else its value in env_file, else None.

    Reads the file without loading it into os.environ. Callers must never print the result.
    """
    value = os.environ.get(name)
    if value:
        return value
    if not Path(env_file).is_file():
        return None
    return dotenv_values(env_file).get(name) or None


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (TAGGING_DIR / p).resolve()


class SearchConfig(BaseModel):
    max_tries_per_code: int
    target_positives: int
    hard_to_find_below: int
    random_share: float
    jev_concurrency: int


class JevConfig(BaseModel):
    url: str
    model: str
    max_retries: int
    backoff_start_seconds: float
    state_note: str


class EmbeddingConfig(BaseModel):
    model: str
    dir: str
    batch_size: int
    usd_per_million_tokens: float

    @property
    def dir_abspath(self) -> Path:
        path = _resolve(self.dir)
        path.mkdir(parents=True, exist_ok=True)
        return path


class Config(BaseModel):
    user_id: int
    cortico_db_path: str
    tagging_db_path: str
    search: SearchConfig
    jev: JevConfig
    embeddings: EmbeddingConfig

    @property
    def cortico_db_abspath(self) -> Path:
        return _resolve(self.cortico_db_path)

    @property
    def tagging_db_abspath(self) -> Path:
        path = _resolve(self.tagging_db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def typesafe_api_key(self) -> str | None:
        for env_file in TYPESAFE_ENV_FILES:
            value = read_env_var("TYPESAFE_API_KEY", env_file)
            if value:
                return value
        return None

    def openai_api_key(self) -> str | None:
        return read_env_var("OPENAI_API_KEY", OPENAI_ENV_FILE)

    def openai_base_url(self) -> str | None:
        return read_env_var("OPENAI_BASE_URL", OPENAI_ENV_FILE)


@lru_cache
def get_config() -> Config:
    with open(BACKEND_DIR / "config.yaml") as f:
        raw = yaml.safe_load(f)
    if os.environ.get("TAGGING_DB_PATH"):
        raw["tagging_db_path"] = os.environ["TAGGING_DB_PATH"]
    if os.environ.get("TAGGING_EMBEDDINGS_DIR"):
        raw["embeddings"]["dir"] = os.environ["TAGGING_EMBEDDINGS_DIR"]
    return Config.model_validate(raw)
