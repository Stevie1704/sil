"""`sil`: one command that names every SiL tool and starts the one asked for.

Each subcommand is the `main` of the module that implements it, called with
the remaining arguments, so its arguments, exit codes and reports are the
module's own. A module is imported only when its subcommand runs: `sil schema
import` needs the `sil[dwarf]` extra, and no other subcommand should.

`sil run` is the native runner itself: `sil` replaces its own process with
`sil-run`, so the Run's exit code, signals and output are the runner's. Only
a diagnostics option (`--diagnostics`, `--diagnostics-out`, `--html`) hands
the Run to sil.diagnostics_run, which starts the diagnostics runner instead.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
from dataclasses import dataclass

PROG = "sil"
EXIT_USAGE = 2
EXIT_NOT_FOUND = 127  # the shell's "command not found"; sil-run uses 0 to 3


@dataclass(frozen=True)
class ModuleCommand:
    """A subcommand: the module whose `main` runs it, and what it does."""

    module: str
    summary: str
    leading: tuple[str, ...] = ()  # arguments put before the user's

    def run(self, path: str, arguments: list[str]) -> int:
        module = importlib.import_module(self.module)
        return module.main([*self.leading, *arguments]) or 0


@dataclass(frozen=True)
class NativeCommand:
    """A subcommand that is a native executable, found on PATH."""

    executable: str
    summary: str

    def run(self, path: str, arguments: list[str]) -> int:
        found = shutil.which(self.executable)
        if found is None:
            sys.stderr.write(f"{path}: error: cannot find {self.executable} on PATH\n")
            return EXIT_NOT_FOUND
        sys.stdout.flush()
        os.execv(found, [self.executable, *arguments])


DIAGNOSTICS_OPTIONS = ("--diagnostics", "--diagnostics-out", "--html")


def requests_diagnostics(arguments: list[str]) -> bool:
    return any(argument in DIAGNOSTICS_OPTIONS
               or argument.startswith(("--diagnostics-out=", "--html="))
               for argument in arguments)


@dataclass(frozen=True)
class RunCommand:
    """`sil run`: the ordinary runner, or the diagnostics Run when asked."""

    ordinary: NativeCommand
    diagnostics: ModuleCommand
    summary: str

    def run(self, path: str, arguments: list[str]) -> int:
        chosen = (self.diagnostics if requests_diagnostics(arguments)
                  else self.ordinary)
        return chosen.run(path, arguments)


@dataclass(frozen=True)
class Group:
    """Subcommands that belong together, under one name."""

    summary: str
    commands: dict[str, ModuleCommand | NativeCommand | RunCommand | Group]


ROOT = Group("SiL: deterministic Runs of virtual ECUs\n\n"
             "The docs of this version are installed: 'sil docs' lists the topics,\n"
             "and 'sil docs agent-guide' is the entry point for a coding agent.\n"
             "'sil skill install' gives a coding agent a skill, so it finds SiL\n"
             "without being told.", {
    "docs": ModuleCommand("sil.docs",
                          "print the docs of this version, offline (start here)"),
    "examples": ModuleCommand("sil.example_files",
                              "list or copy the examples of this version, offline"),
    "check": ModuleCommand("sil.check",
                           "run a Manifest twice and fail on any Recording difference"),
    "compare": ModuleCommand("sil.compare",
                             "compare a Recording with a reference under a contract"),
    "footprint": ModuleCommand("sil.footprint",
                               "report the worst-case payload memory a Manifest declares"),
    "run": RunCommand(
        NativeCommand("sil-run", ""),
        ModuleCommand("sil.diagnostics_run", ""),
        "execute a Manifest into a Recording (the native sil-run); "
        "'sil run --diagnostics --help' lists the measurement options"),
    "benchmark": ModuleCommand("sil.benchmark",
                               "repeat fresh diagnostics Runs of a Manifest "
                               "and summarize them"),
    "profile": ModuleCommand("sil.profile_run",
                             "capture a CPU profile of one fresh Run with "
                             "Linux perf"),
    "diagnostics": Group("present saved diagnostics reports", {
        "render": ModuleCommand("sil.diagnostics_render",
                                "write the standalone HTML report of a "
                                "saved Run or benchmark report"),
    }),
    "bundle": Group("seal and run offline regression bundles", {
        "seal": ModuleCommand("sil.bundle",
                              "record the identity of a bundle and its runtime",
                              ("seal",)),
        "verify": ModuleCommand("sil.bundle",
                                "check every sealed identity without running",
                                ("verify",)),
        "run": ModuleCommand("sil.bundle",
                             "verify a bundle, then execute every declared Run",
                             ("run",)),
        "matrix": ModuleCommand("sil.matrix",
                                "run a list of sealed bundles as one CI job"),
    }),
    "fmi": Group("inspect FMUs and author Runs that use them", {
        "inspect": ModuleCommand("sil.fmi.inspection",
                                 "report whether this importer can drive an FMU"),
        "couple": ModuleCommand("sil.fmi.coupling",
                                "author a Run of FMUs coupled through Channels"),
        "replay": ModuleCommand("sil.fmi.authoring",
                                "author a Run that replays a Recording into one FMU"),
        "substitute": ModuleCommand("sil.fmi.substitution",
                                    "replace one coupled FMU with its Recording"),
    }),
    "manifest": Group("check Manifests", {
        "validate": ModuleCommand("sil.manifest_validate",
                                  "list every error of a Manifest without "
                                  "running it"),
    }),
    "new": Group("write starting points to edit", {
        "participant": ModuleCommand("sil.scaffold",
                                     "write a minimal Participant and a "
                                     "Manifest builder that runs it"),
    }),
    "recording": Group("read Recordings, and prepare them for replay", {
        "channels": ModuleCommand("sil.recording_view",
                                  "list the Channels of a Recording", ("channels",)),
        "show": ModuleCommand("sil.recording_view",
                              "print the decoded Messages of a Recording", ("show",)),
        "query": ModuleCommand("sil.recording_query",
                               "bounded fields, summaries and predicate search"),
        "csv": ModuleCommand("sil.csv_recording",
                             "convert CSV signals into a Recording"),
        "window": ModuleCommand("sil.replay_window",
                                "select and rebase a window of a Recording"),
    }),
    "skill": Group("give a coding agent the skill of this version", {
        "install": ModuleCommand("sil.skill",
                                 "copy the SiL agent skill into a project or "
                                 "the home directory"),
    }),
    "schema": Group("derive Schemas", {
        "import": ModuleCommand("sil.schema_import",
                                "write a flat Schema from the DWARF layout of a C type "
                                "(needs sil[dwarf])"),
    }),
})


def usage(group: Group, path: str) -> str:
    width = max(map(len, group.commands))
    lines = [f"usage: {path} <command> [arguments]", "", group.summary, "",
             "commands:"]
    lines += [f"  {name:<{width}}  {entry.summary}"
              for name, entry in group.commands.items()]
    lines += ["", f"Run '{path} <command> --help' for the arguments of one command."]
    return "\n".join(lines) + "\n"


def dispatch(group: Group, path: str, argv: list[str]) -> int:
    if argv[:1] in (["-h"], ["--help"]):
        sys.stdout.write(usage(group, path))
        return 0
    entry = group.commands.get(argv[0]) if argv else None
    if entry is None:
        problem = f"unknown command '{argv[0]}'" if argv else "a command is required"
        sys.stderr.write(f"{usage(group, path)}{path}: error: {problem}\n")
        return EXIT_USAGE
    name, arguments = argv[0], argv[1:]
    if isinstance(entry, Group):
        return dispatch(entry, f"{path} {name}", arguments)
    return entry.run(f"{path} {name}", arguments)


def main(argv: list[str] | None = None) -> int:
    return dispatch(ROOT, PROG, sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
