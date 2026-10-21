"""核对当前工作区 jar 后停止其 Java 进程，不停止数据库或清空数据。"""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    path = ROOT / "var/business/java.pid"
    if not path.exists():
        return
    pid = int(path.read_text())
    env = dict(os.environ, SUPPORT_STOP_PID=str(pid), SUPPORT_EXPECTED_JAR=str(ROOT / "business/target/support-business-0.1.0.jar"))
    script = """$taskPid=[int]$env:SUPPORT_STOP_PID
$taskProcess=Get-CimInstance Win32_Process -Filter ('ProcessId='+$taskPid)
if($taskProcess){
 if(-not $taskProcess.CommandLine.Contains($env:SUPPORT_EXPECTED_JAR)){throw 'PID does not match workspace jar'}
 Stop-Process -Id $taskPid -Force
}
"""
    if os.name != "nt":
        raise SystemExit("This safe PID verifier currently supports Windows only")
    subprocess.run(["pwsh", "-NoProfile", "-Command", script], env=env, check=True)


if __name__ == "__main__":
    main()
