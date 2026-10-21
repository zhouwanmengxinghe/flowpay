"""通过显式本机代理获取官方 Redis 镜像，再离线导入 Docker；不修改 Docker 全局代理。"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import time
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    proxy = "http://127.0.0.1:7890"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    token_url = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/redis:pull"
    def read(url: str, headers: dict[str, str] | None = None) -> bytes:
        for attempt in range(3):
            try:
                with opener.open(urllib.request.Request(url, headers=headers or {}), timeout=30) as response:
                    return response.read()
            except Exception as error:
                if attempt == 2:
                    raise
                if isinstance(error, urllib.error.HTTPError) and error.code == 401 and headers:
                    headers["Authorization"] = "Bearer " + json.loads(read(token_url))["token"]
                time.sleep(1)
        raise RuntimeError("Registry download failed")
    token = json.loads(read(token_url))["token"]
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.manifest.v1+json"}
    base = "https://registry-1.docker.io/v2/library/redis/"
    index = json.loads(read(base + "manifests/7.4-alpine", headers))
    descriptor = next(row for row in index["manifests"] if row.get("platform", {}).get("os") == "linux" and row["platform"]["architecture"] == "amd64")
    manifest = json.loads(read(base + "manifests/" + descriptor["digest"], headers))
    directory = ROOT / "var/business/redis-image"
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / "redis.tar"
    with tarfile.open(archive, "w") as tar:
        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name); info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        config = manifest["config"]
        def blob(item: dict) -> bytes:
            path = directory / item["digest"].replace(":", "-")
            if not path.exists():
                path.write_bytes(read(base + "blobs/" + item["digest"], headers))
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != item["digest"].split(":")[1]:
                raise ValueError("Registry blob checksum mismatch")
            return data
        config_name = config["digest"].split(":")[1] + ".json"
        add(config_name, blob(config))
        layers = []
        for i, layer in enumerate(manifest["layers"]):
            print("Preparing Redis layer", i + 1, "of", len(manifest["layers"]), flush=True)
            name = f"layer-{i}/layer.tar"
            add(name, gzip.decompress(blob(layer)))
            layers.append(name)
        add("manifest.json", json.dumps([{"Config": config_name, "RepoTags": ["redis:7.4-alpine"], "Layers": layers}]).encode())
    subprocess.run(["docker", "load", "-i", str(archive)], check=True)
    print("Redis image loaded; official manifest digest:", descriptor["digest"])


if __name__ == "__main__":
    main()
