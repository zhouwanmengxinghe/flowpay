"""Generate deployment-only local credentials without overwriting existing configuration."""
from __future__ import annotations

import os
import secrets
from pathlib import Path


def create_config(root: Path) -> bool:
    path = root / ".env.deploy"
    if path.exists():
        return False
    values = {
        "SUPPORT_DB_PASSWORD": secrets.token_hex(24),
        "SUPPORT_AGENT_KEY": secrets.token_hex(32),
        "SUPPORT_DEMO_PASSWORD": secrets.token_hex(16),
        "DEPLOY_PORT": "8080",
        "FLOWPAY_DATASET_ROOT": "./dataset",
        "FLOWPAY_MODEL_PROVIDER": "anthropic-compatible",
        "FLOWPAY_MODEL_BASE_URL": "https://api.deepseek.com/anthropic",
        "FLOWPAY_MODEL_NAME": "deepseek-flash",
        "FLOWPAY_MODEL_API_KEY": "",
        "FLOWPAY_MODEL_REQUEST_OPTIONS": "'{\"max_tokens\":2048,\"thinking\":{\"type\":\"disabled\"}}'",
        "DEPLOY_HTTP_PROXY": "",
        "DEPLOY_HTTPS_PROXY": "",
        "MAVEN_PROXY_HOST": "",
        "MAVEN_PROXY_PORT": "7890",
    }
    # Exclusive creation prevents concurrent launchers from rotating credentials.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("# Local deployment configuration. Do not commit this file.\n")
        handle.writelines(f"{key}={value}\n" for key, value in values.items())
    return True


if __name__ == "__main__":
    created = create_config(Path.cwd())
    print("Created .env.deploy" if created else "Using existing .env.deploy")
