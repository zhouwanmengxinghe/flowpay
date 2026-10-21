"""启动独立业务演示：已有 Java/Maven + Docker，本机不安装 Python 项目依赖。"""
from __future__ import annotations
import argparse
import http.client
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--mode", choices=["mock", "real"], default="real")
    parser.add_argument("--engine", choices=["s4", "langchain"], default="langchain")
    parser.add_argument("--agent-only", action="store_true")
    args = parser.parse_args()
    os.chdir(ROOT)
    path = ROOT / ".env.support"
    if not path.exists():
        path.write_text("SUPPORT_DB_PASSWORD=support-local-only\nSUPPORT_AGENT_KEY=" + secrets.token_hex(32)
            + "\nSUPPORT_AGENT_MODE=mock\nSUPPORT_DEMO=true\nSUPPORT_DEMO_PASSWORD=FlowPay-Demo-2026!\nSUPPORT_ORIGIN=http://localhost:8080\n", encoding="utf-8")
    env = dict(os.environ)
    for config in [ROOT / ".env", path]:
        if config.exists():
            for line in config.read_text(encoding="utf-8-sig").splitlines():
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env[key] = value.strip().strip('"').strip("'")
    env["SUPPORT_AGENT_MODE"] = args.mode
    env["SUPPORT_AGENT_ENGINE"] = args.engine
    if not args.skip_build:
        subprocess.run(["pwsh", "-File", "scripts/business/build.ps1"], check=True, env=env)
    compose = ["docker", "compose", "--env-file", ".env.support", "-f", "compose.business.yaml"]
    if args.mode == "real":
        subprocess.run(["docker", "compose", "--env-file", ".env", "up", "-d", "--wait", "--no-build", "--pull", "never", "postgres", "ollama"],check=True,env=env)
        compose += ["-f", "compose.business.real.yaml"]
    subprocess.run([*compose, "up", "-d", "--wait", "--no-build", "--pull", "never", "support-db", "support-redis", "support-agent"], check=True, env=env)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if args.agent_only:
        import json
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            try:
                with opener.open("http://127.0.0.1:8010/health",timeout=2) as response:
                    health=json.load(response)
                if health.get("mode")==args.mode and health.get("engine")==args.engine:
                    print("Agent ready:",args.mode,args.engine)
                    (ROOT/"var/business/mode.txt").write_text(args.mode+":"+args.engine)
                    return
            except (urllib.error.URLError,TimeoutError,http.client.HTTPException):
                pass
            time.sleep(.5)
        raise SystemExit("Agent readiness deadline exceeded; inspect container logs")
    try:
        with opener.open("http://localhost:8080/api/health", timeout=2):
            raise SystemExit("Port 8080 already serves an application. Stop it before starting another instance.")
    except (urllib.error.URLError, TimeoutError):
        pass
    env.update(SUPPORT_DB_URL="jdbc:postgresql://127.0.0.1:15433/support", SUPPORT_AGENT_URL="http://127.0.0.1:8010")
    directory = ROOT / "var/business"
    directory.mkdir(parents=True, exist_ok=True)
    java = shutil.which("java")
    if not java:
        raise SystemExit("Java 21 is required")
    jar = ROOT / "business/target/support-business-0.1.0.jar"
    if not jar.is_file():
        raise SystemExit("Build business/pom.xml first")
    with (directory / "java.out.log").open("w", encoding="utf-8") as out, (directory / "java.err.log").open("w", encoding="utf-8") as err:
        process = subprocess.Popen([java, "-XX:ActiveProcessorCount=2", "-Xmx512m", "-jar", str(jar)],
            cwd=ROOT, env=env, stdout=out, stderr=err,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    (directory / "java.pid").write_text(str(process.pid))
    (directory / "mode.txt").write_text(args.mode + ":" + args.engine)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit("Java startup failed; inspect var/business/java.out.log")
        try:
            with opener.open("http://localhost:8080/api/health", timeout=1):
                print("Business workbench ready: http://localhost:8080", flush=True)
                print("Local demo accounts: merchant_a, merchant_b, support_a, support_b. Password is in .env.support.")
                return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(.5)
    raise SystemExit("Startup deadline exceeded; inspect var/business/java.out.log")


if __name__ == "__main__":
    main()
