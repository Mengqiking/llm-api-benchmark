"""Run the offline demo directly from a checkout, with no installation."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm_bench.cli import main

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(["demo", *sys.argv[1:]]))
