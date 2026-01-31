"""
config.py - Simplified configuration model for Oatgrass
"""

from pathlib import Path
from typing import Dict, Optional
from pydantic import BaseModel, Field
from rich.console import Console
import sys

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

console = Console()


class APIKeysConfig(BaseModel):
    discogs_key: str = ""

class FuzzySearchConfig(BaseModel):
    """Parameters that control the fuzzy search heuristics."""

    min_similarity: int = Field(
        default=60,
        description="Minimum similarity score (0-100) to consider a fuzzy match acceptable"
    )
    substring_depth: int = Field(
        default=3,
        description="How many shortened substring variants to emit for fallback searches"
    )
    redirects: Dict[str, str] = Field(
        default_factory=dict,
        description="Optional redirect map (source name → fallback name) used when automatic matches fail"
    )


class TrackerConfig(BaseModel):
    name: str
    url: str
    api_key: str = ""


class OatgrassConfig(BaseModel):
    api_keys: APIKeysConfig = Field(default_factory=APIKeysConfig)
    trackers: Dict[str, TrackerConfig] = Field(default_factory=dict)
    fuzzy_search: FuzzySearchConfig = Field(default_factory=FuzzySearchConfig)
    config_path: Optional[Path] = None


def load_config(config_path: Path) -> OatgrassConfig:
    """Load configuration from TOML file"""
    
    if not config_path.exists():
        console.print(f"[red][ERROR][/red] Configuration file not found: {config_path}")
        console.print("Please create config.toml with your API keys")
        sys.exit(1)
    
    try:
        with open(config_path, "rb") as f:
            config_data = tomllib.load(f)
            
        # Create config instance
        config = OatgrassConfig(
            api_keys=APIKeysConfig(**config_data.get("api_keys", {})),
            trackers={
                name: TrackerConfig(**tracker_data) 
                for name, tracker_data in config_data.get("trackers", {}).items()
            },
            fuzzy_search=FuzzySearchConfig(**config_data.get("fuzzy_search", {})),
            config_path=config_path
        )
        
        return config
        
    except Exception as e:
        console.print(f"[red][ERROR][/red] Error loading configuration: {e}")
        sys.exit(1)
