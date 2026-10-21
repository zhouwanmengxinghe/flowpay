"""Deployment initialization only; never sends a chat-model request."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path


def validate_dataset(root: Path) -> None:
    required = ("data/knowledge", "benchmark/design", "postgres/flowpay_knowledge.dump")
    missing = [name for name in required if not (root / name).exists()]
    if missing:
        raise ValueError("Incomplete unpacked dataset: " + ", ".join(missing))
    if not (root / "data/generated/canonical/manifest.json").exists():
        if not (root / "src/flowpay_simulator").is_dir():
            raise ValueError("Dataset has no business snapshot or src/flowpay_simulator")


def initialize_dataset(root: Path) -> None:
    validate_dataset(root)
    if (root / "data/generated/canonical/manifest.json").exists():
        print("Business snapshot already present; preserving it.", flush=True)
        return
    subprocess.run([sys.executable, "-m", "flowpay_simulator.cli", "simulate"], cwd=root,
                   env={**os.environ, "PYTHONPATH": str(root / "src")}, check=True)
    if not (root / "data/generated/canonical/manifest.json").is_file():
        raise ValueError("Simulator did not produce its completion manifest")


def initialize_embedding() -> None:
    with Path("backend/configs/local.toml").open("rb") as handle:
        embedding = tomllib.load(handle)["embedding"]
    # Ollama is internal; explicitly bypass any external HTTP proxy here.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = "http://ollama:11434"
    def digest() -> str | None:
        with opener.open(base + "/api/tags", timeout=10) as response:
            models = json.load(response)["models"]
        return next((item["digest"].removeprefix("sha256:") for item in models
                     if item["name"] == embedding["name"]), None)
    if digest() is None:
        print("Downloading embedding model (first deployment only)...", flush=True)
        request = urllib.request.Request(base + "/api/pull", method="POST",
            data=json.dumps({"name": embedding["name"], "stream": True}).encode(),
            headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=1200) as response:
            for line in response:
                event = json.loads(line)
                if event.get("error"):
                    raise RuntimeError("Ollama model download failed: " + event["error"])
    if digest() != embedding["revision"]:
        raise ValueError("Embedding digest differs from the knowledge snapshot; refusing to start")
    print("Embedding model ready; revision verified.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "dataset", "embedding"))
    action = parser.parse_args().action
    if action == "embedding":
        initialize_embedding()
    elif action == "dataset":
        initialize_dataset(Path("/dataset"))
    else:
        validate_dataset(Path("/dataset"))
        print("Dataset layout valid. No chat-model request sent.")
