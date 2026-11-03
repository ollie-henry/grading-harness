#!/usr/bin/env python3
"""Marks one coding answer for the Ollie Henry Hub's grading runs.

A computation job runs this in the hub runner's sandbox (no network, read-only code and inputs, one writable output
folder). It takes two inputs:
  answer  the model's whole reply; its last ```python fenced block is the solution
  tests   the task's tests: a Python file of test_* functions with plain asserts, importing from `solution`
and writes /job/out/scores.json, one flat JSON object of numbers:
  score     1 when every test passed, else 0
  passed    how many test_* functions passed
  total     how many there were
  no_code   1 when the reply had no ```python block
  timed_out 1 when the tests ran past the time limit
It always exits 0 once it has written the scores, so a wrong answer is a mark of 0, never a failed job.

Standard library only. Nothing here holds a task, a test or an answer: those travel sealed inside each run.

What it can't do: stop an answer written to cheat it. The answer runs in the process that counts the tests, so code that
set out to fake the counts could. The answers it marks are models' replies to made-up tasks, written without sight of
this script, and their marks choose nothing; a sturdier marker would run each test where the answer can't reach.
"""

import json
import os
import re
import subprocess
import sys
import tempfile

INPUTS = os.path.abspath(os.environ.get("HUB_JOB_INPUTS", "/job/inputs"))
OUT = os.path.abspath(os.environ.get("HUB_JOB_OUT", "/job/out"))
TOTAL_SECONDS = 120  # the whole test file
TEST_SECONDS = 20  # each test_* function
MEMORY_BYTES = 2 * 1024**3

FENCE = re.compile(r"```[ \t]*(?:python3?|py)[ \t]*\r?\n(.*?)```", re.S | re.I)

# Runs in its own process, held to 2 GB of memory, 512 processes and threads and 64 MB a file, so a runaway answer
# can't use up the runner computer. In the sandbox's own user namespace only the job's processes count towards the 512;
# outside one the user's others count too, which is why it isn't lower. Imports the tests (which import `solution` from
# the work folder), then runs each test_* function in name order, each under its own alarm. Writes its counts to a
# result file whose name the solution isn't given, so nothing the solution prints can pass for them; its own output goes
# nowhere.
CHILD = r"""
import json, resource, signal, sys
work, tests, per_test, memory, result = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
def report(counts):
    with open(result, "w", encoding="utf-8") as f:
        json.dump(counts, f)
for limit, value in ((resource.RLIMIT_AS, memory), (resource.RLIMIT_NPROC, 512), (resource.RLIMIT_FSIZE, 64 * 1024 * 1024)):
    try:
        resource.setrlimit(limit, (value, value))
    except (ValueError, OSError):
        pass
sys.path.insert(0, work)
sys.setrecursionlimit(10000)
class Slow(BaseException):
    pass
def slow(signum, frame):
    raise Slow()
signal.signal(signal.SIGALRM, slow)
ns = {"__name__": "hub_tests"}
try:
    with open(tests, encoding="utf-8") as f:
        code = compile(f.read(), "tests.py", "exec")
    signal.alarm(per_test)
    exec(code, ns)
    signal.alarm(0)
except BaseException as e:
    signal.alarm(0)
    report({"passed": 0, "total": 0, "load_error": 1})
    sys.exit(0)
names = sorted(n for n, v in ns.items() if n.startswith("test_") and callable(v))
passed = 0
for n in names:
    try:
        signal.alarm(per_test)
        ns[n]()
        signal.alarm(0)
        passed += 1
    except BaseException:
        signal.alarm(0)
report({"passed": passed, "total": len(names)})
"""


def write_scores(scores):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "scores.json"), "w", encoding="utf-8") as f:
        json.dump(scores, f, allow_nan=False, sort_keys=True)


def main():
    with open(os.path.join(INPUTS, "answer"), encoding="utf-8", errors="replace") as f:
        answer = f.read()
    blocks = FENCE.findall(answer)
    if not blocks:
        write_scores({"score": 0, "passed": 0, "total": 0, "no_code": 1, "timed_out": 0})
        return
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR", "/tmp")) as work:
        with open(os.path.join(work, "solution.py"), "w", encoding="utf-8") as f:
            f.write(blocks[-1])
        child = os.path.join(work, "_child.py")
        with open(child, "w", encoding="utf-8") as f:
            f.write(CHILD)
        tests = os.path.join(INPUTS, "tests")
        timed_out = 0
        counts = {"passed": 0, "total": 0}
        result = os.path.join(work, "result-" + os.urandom(12).hex() + ".json")
        try:
            subprocess.run(
                [sys.executable, "-I", child, work, tests, str(TEST_SECONDS), str(MEMORY_BYTES), result],
                cwd=work,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=TOTAL_SECONDS,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONHASHSEED": "0", "LANG": "C.UTF-8"},
            )
            if os.path.exists(result):
                with open(result, encoding="utf-8") as f:
                    counts = json.load(f)
        except subprocess.TimeoutExpired:
            timed_out = 1
        except (ValueError, OSError):
            pass
        passed = int(counts.get("passed", 0))
        total = int(counts.get("total", 0))
        ok = 1 if total > 0 and passed == total and not timed_out else 0
        write_scores({"score": ok, "passed": max(passed, 0), "total": max(total, 0), "no_code": 0, "timed_out": timed_out})


if __name__ == "__main__":
    main()
