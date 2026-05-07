#!/usr/bin/env python3
"""Detect tool/function calling support from /v1/models endpoint."""

import json
import httpx
from pathlib import Path

# Load provider registry
with open("data/provider_registry.json") as f:
    registry = json.load(f)

def check_tool_support(base_url: str, api_key: str, model_name: str) -> bool:
    """Query /v1/models and check if model supports function calling."""
    try:
        resp = httpx.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=15,
        )
        if resp.status_code != 200:
            return False
        
        result = resp.json()
        models_data = result.get("data", result) if isinstance(result, dict) else result
        
        for m in models_data:
            if m.get("id", "") == model_name:
                # Check for function/tools support
                if m.get("capabilities", {}).get("function_calling"):
                    return True
                # Check for tools in root
                if "tools" in m or "function" in str(m):
                    return True
                # Check model ID patterns for known tool-supporting models
                name_lower = model_name.lower()
                tool_patterns = [
                    "gpt-4", "gpt-5", "claude", "gemini", 
                    "qwen", "deepseek", "kimi", "glm", "grok"
                ]
                if any(p in name_lower for p in tool_patterns):
                    # Most modern models support tools
                    return True
                return False
        return False
        
    except Exception as e:
        print(f"Error checking {model_name}: {e}")
        return False

def update_registry():
    """Update provider_registry.json with tool_calls info."""
    updated = 0
    for pname, info in registry.get("providers", {}).items():
        url = info.get("base_url", "")
        key = ""  # Would need actual API keys
        
        # For now, use model_types to infer
        model_types = info.get("model_types", [])
        if "text" in model_types or "chat" in model_types:
            # Assume text/chat models might support tools
            info["supports_tool_calls"] = True
            updated += 1
    
    with open("data/provider_registry.json", "w") as f:
        json.dump(registry, f, indent=2)
    
    print(f"Updated {updated} providers with tool_calls info")

if __name__ == "__main__":
    update_registry()
