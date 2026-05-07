#!/usr/bin/env python3

import json
import os
from pathlib import Path

OPENCODE = Path.home() / ".config/opencode/opencode.json"

def load_config():
    if OPENCODE.exists():
        return json.loads(OPENCODE.read_text())
    return {}

def save_config(config):
    OPENCODE.write_text(json.dumps(config, indent=2))

def add_secrets(secrets_dict):
    config = load_config()
    added = 0
    
    for pname, api_key in secrets_dict.items():
        if "provider" not in config:
            config["provider"] = {}
        if pname not in config["provider"]:
            config["provider"][pname] = {}
        
        config["provider"][pname]["apiKey"] = api_key
        print(f"Added/updated key for {pname}")
        added += 1
    
    save_config(config)
    print(f"\nTotal: {added} provider(s) updated")
    print(f"Config saved to {OPENCODE}")

def from_env_vars(prefix="LLM_"):
    secrets = {}
    for key, value in os.environ.items():
        if key.startswith(prefix):
            provider = key[len(prefix):].lower()
            secrets[provider] = value
    return secrets

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            secrets = json.load(f)
        add_secrets(secrets)
    else:
        secrets = from_env_vars()
        if secrets:
            print(f"Found {len(secrets)} API keys in environment")
            add_secrets(secrets)
        else:
            print("Usage: python bulk_add_secrets.py <secrets.json>")
            print("Or set env vars like: LLM_OPENAI=your-key-here LLM_GEMINI=your-key-here")
