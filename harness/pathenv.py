#!/usr/bin/env python3
"""pathenv.py: PATH belongs to a build request, rather than a second worker (#805).

Usage: pathenv.py [INPUT], one JSON object inline, in a file, or '-' for stdin:
    {"blaze": "/path/to/blaze", "work": "/new/scratch/directory"}

Identical PATH, an empty prefix and restoring PATH must reuse the loaded worker. A script
that selects a tool through PATH must see each command's tool and environment. Each result
is compared with a fresh reference Cargo build. Worker PIDs, directory identities, loads,
reloads, output and wall times are recorded in work/results.json; times do not gate.
"""
import dataclasses
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from blaze_cli import BLAZE, TOOLCHAIN, base_env


@dataclasses.dataclass(frozen=True)
class Input:
    blaze: str = ""
    work: str = ""


def typed(argument):
    if argument == "-":
        argument = sys.stdin.read()
    elif argument and not argument.lstrip().startswith("{"):
        argument = Path(argument).read_text()
    try:
        value = json.loads(argument or "{}")
    except json.JSONDecodeError as error:
        sys.exit(f"pathenv.py: INPUT is not JSON: {error}")
    if type(value) is not dict:
        sys.exit("pathenv.py: INPUT is a JSON object")
    known = {field.name for field in dataclasses.fields(Input)}
    for name, item in value.items():
        if name not in known:
            sys.exit(f"pathenv.py: INPUT has no field {name}")
        if type(item) is not str:
            sys.exit(f"pathenv.py: INPUT field {name} is a string")
    return Input(**value)


PLAIN = {
    "Cargo.toml": '[package]\nname = "path-env-plain"\nversion = "0.0.0"\nedition = "2021"\n',
    "src/main.rs": 'fn main() { println!("same build"); }\n',
}

SCRIPT = {
    "Cargo.toml": '[package]\nname = "path-env-script"\nversion = "0.0.0"\nedition = "2021"\n'
                  '\n[workspace.metadata.blaze.sandbox]\nbuild-script-env = ["SCRIPT_VALUE"]\n',
    # PATH deliberately is not declared: selecting a different executable is still an input.
    "build.rs": '''use std::{env, process::Command};
fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-env-changed=SCRIPT_VALUE");
    let tool = Command::new("blaze-path-tool").output().expect("tool on PATH");
    assert!(tool.status.success());
    let tool = String::from_utf8(tool.stdout).unwrap();
    let value = env::var("SCRIPT_VALUE").unwrap_or_else(|_| "unset".to_owned());
    println!("cargo:rustc-env=SELECTED_TOOL={}", tool.trim());
    println!("cargo:rustc-env=SCRIPT_VALUE={value}");
}
''',
    "src/main.rs": 'fn main() { println!("{}:{}", env!("SELECTED_TOOL"), env!("SCRIPT_VALUE")); }\n',
}


def write_workspace(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def main(config):
    if os.name == "nt":
        sys.exit("pathenv.py: the sandboxed tool fixture needs Linux or macOS")
    blaze = Path(config.blaze or Path(BLAZE).with_name("blaze")).resolve()
    if not blaze.is_file():
        sys.exit(f"pathenv.py: no compiler at {blaze}")
    if config.work:
        work = Path(config.work).absolute()
        work.mkdir(parents=True, exist_ok=False)
    else:
        work = Path(tempfile.mkdtemp(prefix="rj-pathenv-"))
    print(f"work: {work}", flush=True)
    results, failed = [], []
    empty = work / "empty"
    empty.mkdir()
    tools = []
    for name in ("first", "second"):
        directory = work / name
        directory.mkdir()
        tool = directory / "blaze-path-tool"
        tool.write_text(f"#!/bin/sh\nprintf '%s\\n' '{name}'\n")
        tool.chmod(0o755)
        tools.append(directory)

    def check(label, held, detail=""):
        print(f"{'PASS' if held else 'FAIL'} {label}" + (f": {detail}" if detail else ""), flush=True)
        if not held:
            failed.append(label)

    def run(argv, ws, env):
        started = time.monotonic()
        ran = subprocess.run(argv, cwd=ws, env=env, capture_output=True, text=True, timeout=600)
        return ran, time.monotonic() - started

    def sequence(name, files, steps):
        ws, home = work / name, work / (name + "-home")
        write_workspace(ws, files)
        base = base_env(str(home))
        base.update(BLAZE_SPARE="0", BLAZE_MEMORY_MAX="off", BLAZE_IDLE_SECS="3600")
        base["PATH"] = str(blaze.parent) + os.pathsep + base["PATH"]
        base.pop("SCRIPT_VALUE", None)
        cargo = shutil.which("cargo", path=base["PATH"])
        if cargo is None:
            raise RuntimeError("the pinned Cargo reference is not on PATH")
        first_workers = None
        first_loads = None
        try:
            for label, prefix, value in steps:
                env = dict(base)
                env["PATH"] = os.pathsep.join([str(path) for path in prefix] + [base["PATH"]])
                if value is not None:
                    env["SCRIPT_VALUE"] = value
                reference = dict(env, CARGO_TARGET_DIR=str(work / "reference" / name / label))
                expected, _ = run([cargo, f"+{TOOLCHAIN}", "run", "--offline", "-q"], ws, reference)
                check(f"{name}/{label}: reference builds", expected.returncode == 0, expected.stderr[-500:] if expected.returncode else "")
                ran, elapsed = run([str(blaze), "run", "-q"], ws, env)
                (work / f"{name}-{label}.stdout").write_text(ran.stdout)
                (work / f"{name}-{label}.stderr").write_text(ran.stderr)
                status, _ = run([str(blaze), "status"], ws, env)
                (work / f"{name}-{label}.status").write_text(status.stdout + status.stderr)
                workers = sorted(int(pid) for pid in re.findall(r"worker: pid (\d+)", status.stdout))
                identities = sorted(path.name for path in (home / "workers").iterdir()) if (home / "workers").is_dir() else []
                logs = [path.read_text(errors="replace") for path in home.glob("workers/*/log")]
                loads = sum(text.count("load workspace:") for text in logs)
                reloads = sum(text.count("reload:") for text in logs)
                record = dict(case=name, step=label, seconds=elapsed, workers=workers,
                              identities=identities, loads=loads, reloads=reloads,
                              status=status.stdout, exit=ran.returncode, stdout=ran.stdout,
                              reference_exit=expected.returncode, reference_stdout=expected.stdout)
                results.append(record)
                print(json.dumps(record), flush=True)
                check(f"{name}/{label}: agrees with Cargo", expected.returncode == 0 and
                      (ran.returncode, ran.stdout) == (expected.returncode, expected.stdout),
                      f"exit {ran.returncode}, {ran.stdout!r}" + (f", {ran.stderr[-500:]}" if ran.returncode else ""))
                if first_workers is None:
                    first_workers, first_loads = workers, loads
                    check(f"{name}: one initial worker", len(workers) == 1, str(workers))
                else:
                    check(f"{name}/{label}: same worker", bool(first_workers) and workers == first_workers, f"{first_workers} -> {workers}")
                    if name == "plain":
                        check(f"{name}/{label}: workspace kept loaded", bool(first_workers) and loads == first_loads and reloads == 0,
                              f"loads {first_loads} -> {loads}, reloads {reloads}")
        finally:
            run([str(blaze), "stop"], ws, base)

    try:
        sequence("plain", PLAIN, [
            ("initial", [], None), ("identical", [], None),
            ("empty-prefix", [empty], None), ("restored", [], None),
        ])
        sequence("script", SCRIPT, [
            ("initial", [tools[0]], "alpha"), ("identical", [tools[0]], "alpha"),
            ("empty-prefix", [empty, tools[0]], "alpha"),
            ("different-tool", [tools[1]], "alpha"),
            ("different-value", [tools[1]], "beta"),
            ("unset-value", [tools[1]], None),
            ("restored", [tools[0]], "alpha"),
        ])
    finally:
        (work / "results.json").write_text(json.dumps(dict(results=results, failures=failed), indent=2) + "\n")
    print("PASS" if not failed else "FAIL: " + "; ".join(failed), flush=True)
    if not config.work and not failed:
        shutil.rmtree(work)
    return int(bool(failed))


if __name__ == "__main__":
    if len(sys.argv) > 2:
        sys.exit("pathenv.py: expected one JSON input")
    raise SystemExit(main(typed(sys.argv[1] if len(sys.argv) == 2 else "{}")))
