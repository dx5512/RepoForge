"""
Configuration Management for RepoForge

Provides type-safe access to application configuration with environment variable loading,
validation, and sensible defaults.
"""

import os
import logging
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv(override=True)


@dataclass
class Config:
    """Application configuration with type safety and validation."""

    # Required Configuration (must come first, no defaults)
    openai_api_key: str
    feishu_app_id: str
    feishu_app_secret: str

    # Optional Configuration with defaults
    openai_base_url: str = "https://api.deepseek.com"
    model_id: str = "deepseek-chat"
    github_token: Optional[str] = None
    github_repo: Optional[str] = None  # Default repo for PR creation
    target_repo_path: Optional[str] = field(default_factory=lambda: os.getenv("TARGET_REPO_PATH"))

    # Logging
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    # Task Settings
    max_iterations: int = field(default_factory=lambda: int(os.getenv("MAX_ITERATIONS", "20")))
    max_reflection_loops: int = field(default_factory=lambda: int(os.getenv("MAX_REFLECTION_LOOPS", "3")))
    default_timeout: int = field(default_factory=lambda: int(os.getenv("DEFAULT_TIMEOUT", "120")))

    # Docker
    docker_image: str = field(default_factory=lambda: os.getenv("DOCKER_IMAGE", "python:3.11-slim"))

    # Worktree Settings
    worktrees_base: str = field(default_factory=lambda: os.getenv("WORKTREES_BASE", ".feishu_worktrees"))
    dynamic_repos_base: str = field(default_factory=lambda: os.getenv("DYNAMIC_REPOS_BASE", ".dynamic_repos"))

    def __post_init__(self):
        """Validate configuration after initialization."""
        # Validate log level
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.log_level.upper() not in valid_levels:
            raise ValueError(f"Invalid LOG_LEVEL: {self.log_level}. Must be one of {valid_levels}")

        # Set logging level
        logging.getLogger().setLevel(getattr(logging, self.log_level.upper()))

    @classmethod
    def load(cls) -> "Config":
        """
        Load configuration from environment variables.

        Raises:
            ValueError: If required configuration is missing

        Returns:
            Config instance
        """
        required_vars = {
            "OPENAI_API_KEY": "openai_api_key",
            "FEISHU_APP_ID": "feishu_app_id",
            "FEISHU_APP_SECRET": "feishu_app_secret",
        }

        env_values = {}
        missing = []

        for env_var, field_name in required_vars.items():
            value = os.getenv(env_var)
            if not value or value == f"your_{env_var.lower()}_here":
                missing.append(env_var)
            else:
                env_values[field_name] = value

        if missing:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing)}\n"
                f"Please configure them in your .env file."
            )

        # Optional variables with defaults
        env_values["openai_base_url"] = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")
        # Support both MODEL_ID and OPENAI_MODEL_NAME for compatibility
        model_id = os.getenv("MODEL_ID") or os.getenv("OPENAI_MODEL_NAME") or "deepseek-chat"
        env_values["model_id"] = model_id
        env_values["github_token"] = os.getenv("GITHUB_TOKEN")
        env_values["github_repo"] = os.getenv("GITHUB_REPO")

        return cls(**env_values)


# Global config instance (lazy loaded)
_config: Optional[Config] = None


def get_config() -> Config:
    """
    Get the global configuration instance.

    Returns:
        Config instance, loading from environment if not yet loaded
    """
    global _config
    if _config is None:
        _config = Config.load()
    return _config
