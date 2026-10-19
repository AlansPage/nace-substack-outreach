import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_config(path=None):
    config = json.loads((ROOT / "substack_outreach/config.example.json").read_text(encoding="utf-8"))
    if path:
        config.update(json.loads(Path(path).read_text(encoding="utf-8-sig")))
    for key in ("batch_size", "shortlist_size", "max_requests_per_run", "max_output_tokens"):
        if not isinstance(config[key], int) or config[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    if not 1 <= config["max_articles"] <= 5 or not 0 <= config["max_retries"] <= 3:
        raise ValueError("max_articles must be 1..5; max_retries must be 0..3")
    if config["provider"] not in ("mock", "openai"):
        raise ValueError("Supported writing adapters: mock, openai")
    for key in ('database', 'registry_path'):
        target = Path(config[key]).resolve()
        public = (ROOT / 'public').resolve()
        if target == public or public in target.parents:
            raise ValueError(f'{key} cannot be inside public static assets')
    if config['budget_usd_per_run'] < 0 or config['cache_hours'] < 0 or config['request_interval_seconds'] < 0:
        raise ValueError('Budget, cache lifetime and request interval cannot be negative')
    sender = config.get('sender', {})
    if not sender.get('name') or not sender.get('email') or sender.get('company') != 'Nace AI':
        raise ValueError('Configure the complete sender name, Nace AI company and actual email together')
    return config
