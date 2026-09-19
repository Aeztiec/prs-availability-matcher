"""python -m tests  runs every test module and reports which failed."""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main():
    modules = sorted(f[:-3] for f in os.listdir(HERE)
                     if f.startswith("test_") and f.endswith(".py"))
    failed = []
    for name in modules:
        result = subprocess.run(
            [sys.executable, "-m", "tests." + name], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        print("{:<5} {}".format("ok" if result.returncode == 0 else "FAIL", name))
        if result.returncode != 0:
            failed.append(name)
            print(result.stdout[-1500:] + result.stderr[-1500:])
    print("\n{} of {} test files passed".format(len(modules) - len(failed), len(modules)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
