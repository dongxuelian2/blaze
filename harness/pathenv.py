#!/usr/bin/env python3
"""pathenv.py: PATH belongs to a build request, rather than a second worker (#805).

Usage: pathenv.py [INPUT], one JSON object inline, in a file, or '-' for stdin:
    {"blaze": "/path/to/blaze", "work": "/new/scratch/directory"}

Identical PATH, an empty prefix and restoring PATH must reuse the loaded worker. Scripts,
native proc macros and an external assembler use each command's tool and environment,
including an unset PATH. Each result is compared with a fresh reference Cargo build.
Worker PIDs, directory identities, loads, reloads, output and wall times are recorded in
work/results.json; times do not gate. The external assembler fixture is Linux x86-64.
"""
import dataclasses
import json
import os
from pathlib import Path
import re
import shlex
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

NATIVE = {
    "Cargo.toml": '[package]\nname = "path-env-native"\nversion = "0.0.0"\nedition = "2021"\n'
                  '[dependencies]\npath-env-macros = { path = "macros" }\n[workspace]\n',
    "src/main.rs": 'fn main() { println!("{}", path_env_macros::selected!()); }\n',
    "macros/Cargo.toml": '[package]\nname = "path-env-macros"\nversion = "0.0.0"\nedition = "2021"\n'
                         '[lib]\nproc-macro = true\n',
    "macros/src/lib.rs": '''use proc_macro::TokenStream;
#[proc_macro]
pub fn selected(_: TokenStream) -> TokenStream {
    let selected = if std::env::var_os("PATH").is_none() {
        "unset".to_owned()
    } else {
        let tool = std::process::Command::new("blaze-path-tool").output().unwrap();
        assert!(tool.status.success());
        String::from_utf8(tool.stdout).unwrap().trim().to_owned()
    };
    format!("{selected:?}").parse().unwrap()
}
''',
}

ASSEMBLY = dict(PLAIN, **{
    "src/main.rs": '''fn main() {
    let value: usize;
    unsafe { core::arch::asm!("mov {value}, 17", value = out(reg) value); }
    println!("{value}");
}
''',
})


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
    assembler_log = work / "assembler.log"
    assembly_case = sys.platform.startswith("linux") and os.uname().machine in ("x86_64", "amd64")
    clang = shutil.which("clang")
    if assembly_case and clang is None:
        sys.exit("pathenv.py: the external assembler fixture requires clang")
    for name in ("first", "second"):
        directory = work / name
        directory.mkdir()
        tool = directory / "blaze-path-tool"
        tool.write_text(f"#!/bin/sh\nprintf '%s\\n' '{name}'\n")
        tool.chmod(0o755)
        if clang:
            wrapper = directory / "clang"
            wrapper.write_text(f"#!/bin/sh\nprintf '%s\\n' '{name}' >> {shlex.quote(str(assembler_log))}\n"
                               f"exec {shlex.quote(clang)} \"$@\"\n")
            wrapper.chmod(0o755)
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
        if name == "native":
            # The macro sandbox can read its workspace; keep the selected tools inside it.
            for directory in tools:
                shutil.copytree(directory, ws / directory.name)
        base = base_env(str(home))
        base.update(BLAZE_SPARE="0", BLAZE_MEMORY_MAX="off", BLAZE_IDLE_SECS="3600")
        base["PATH"] = str(blaze.parent) + os.pathsep + base["PATH"]
        base.pop("SCRIPT_VALUE", None)
        if name == "native":
            base["BLAZE_PROC_MACRO_WASM"] = "0"
        if name == "assembler":
            base["BLAZE_ASSEMBLER"] = "cc"
        rustup = shutil.which("rustup", path=base["PATH"])
        if rustup is None:
            raise RuntimeError("rustup is needed to locate the pinned reference tools")
        def reference_tool(name):
            found, _ = run([rustup, "which", "--toolchain", TOOLCHAIN, name], ws, base)
            if found.returncode:
                raise RuntimeError(found.stderr)
            return found.stdout.strip()
        cargo, rustc = reference_tool("cargo"), reference_tool("rustc")
        first_workers = None
        first_loads = None
        try:
            for label, prefix, value in steps:
                env = dict(base)
                if prefix is None:
                    env.pop("PATH", None)
                else:
                    if name == "native":
                        prefix = [ws / path.name if path in tools else path for path in prefix]
                    env["PATH"] = os.pathsep.join([str(path) for path in prefix] + [base["PATH"]])
                if value is not None:
                    env["SCRIPT_VALUE"] = value
                # Calling Cargo directly preserves an absent PATH; the rustup proxy adds
                # its toolchain's bin directory, which would change the reference input.
                reference = dict(env, RUSTC=rustc, CARGO_TARGET_DIR=str(work / "reference" / name / label))
                expected, _ = run([cargo, "run", "--offline", "-q"], ws, reference)
                check(f"{name}/{label}: reference builds", expected.returncode == 0, expected.stderr[-500:] if expected.returncode else "")
                assembled_before = assembler_log.read_text().splitlines() if assembler_log.exists() else []
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
                if name == "assembler":
                    assembled = assembler_log.read_text().splitlines() if assembler_log.exists() else []
                    used = assembled[len(assembled_before):]
                    record["assembler_runs"] = used
                    if label == "identical":
                        check(f"{name}/{label}: compiled state reused", not used, repr(used))
                    else:
                        selected = "second" if label == "different-tool" else "first"
                        check(f"{name}/{label}: current tool assembled", bool(used) and set(used) == {selected}, repr(used))
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
        sequence("native", NATIVE, [
            ("initial", [tools[0]], None), ("identical", [tools[0]], None),
            ("empty-prefix", [empty, tools[0]], None),
            ("different-tool", [tools[1]], None), ("unset-path", None, None),
            ("restored", [tools[0]], None),
        ])
        if assembly_case:
            sequence("assembler", ASSEMBLY, [
                ("initial", [tools[0]], None), ("identical", [tools[0]], None),
                ("different-tool", [tools[1]], None), ("restored", [tools[0]], None),
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
