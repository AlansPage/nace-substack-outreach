import json
from pathlib import Path

from .models import check_local_base_url
from .sources import check_default_permission

ROOT = Path(__file__).resolve().parent.parent


HOSTED_FIXED = {"database": "env:DATABASE_URL", "registry_path": "database", "hosted": True}


def load_hosted_config(environ):
    """Vercel: settings from the SUBSTACK_CONFIG env var; storage and facts always come from the shared database."""
    try:
        overrides = json.loads(environ.get("SUBSTACK_CONFIG") or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"SUBSTACK_CONFIG is not valid JSON ({exc})") from None
    if not isinstance(overrides, dict):
        raise ValueError("SUBSTACK_CONFIG must be a JSON object")
    config = load_config(overrides={k: v for k, v in overrides.items() if k not in HOSTED_FIXED} | HOSTED_FIXED)
    if config["provider"] == "local":
        raise ValueError("provider local needs a model on the same machine; use mock or openai in the hosted workspace")
    return config


def load_config(path=None, overrides=None):
    config = json.loads((ROOT / "substack_outreach/config.example.json").read_text(encoding="utf-8"))
    config["hosted"] = False
    if path:
        config.update(json.loads(Path(path).read_text(encoding="utf-8-sig")))
    if overrides:
        config.update(overrides)
    for key in ("batch_size", "shortlist_size", "max_requests_per_run", "max_output_tokens"):
        if not isinstance(config[key], int) or config[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    if not 1 <= config["max_articles"] <= 5 or not 0 <= config["max_retries"] <= 3:
        raise ValueError("max_articles must be 1..5; max_retries must be 0..3")
    if config["provider"] not in ("mock", "openai", "local"):
        raise ValueError("Supported writing adapters: mock, openai, local")
    check_local_base_url(config["local_base_url"])
    for key in ('database', 'registry_path', 'dnc_path'):
        target = Path(config[key]).resolve()
        public = (ROOT / 'public').resolve()
        if target == public or public in target.parents:
            raise ValueError(f'{key} cannot be inside public static assets')
    if config['budget_usd_per_run'] < 0 or config['cache_hours'] < 0 or config['request_interval_seconds'] < 0:
        raise ValueError('Budget, cache lifetime and request interval cannot be negative')
    check_default_permission(config.get('default_feed_permission'))
    if not isinstance(config.get('collect_contacts'), bool):
        raise ValueError('collect_contacts must be true or false')
    sender = config.get('sender', {})
    if not sender.get('name') or not sender.get('email') or sender.get('company') != 'Nace AI':
        raise ValueError('Configure the complete sender name, Nace AI company and actual email together')
    return config
