"""Print a read-only, reproducible snapshot for ChatGPT/Codex handoff."""
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


for label, command in (
    ("branch/status", ["git", "status", "--short", "--branch"]),
    ("recent commits", ["git", "log", "-5", "--pretty=format:%h %s (%ci)"]),
    ("remotes", ["git", "remote", "-v"]),
):
    print(f"\n### {label}")
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    print((result.stdout or result.stderr or "(none)").strip())

print("\n### shared project state")
print((ROOT / "docs" / "PROJECT_STATE.md").read_text(encoding="utf-8"))
