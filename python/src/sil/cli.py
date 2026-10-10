"""`sil`: one command that names every SiL tool and starts the one asked for.

Each subcommand is the `main` of the module that implements it, called with
the remaining arguments, so its arguments, exit codes and reports are the
module's own. A module is imported only when its subcommand runs: `sil schema
import` needs the `sil[dwarf]` extra, and no other subcommand should.

`sil run` is the native runner itself: `sil` replaces its own process with
`sil-run`, so the Run's exit code, signals and output are the runner's.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
from dataclasses import dataclass, field

PROG = "sil"
EXIT_USAGE = 2
EXIT_NOT_FOUND = 127  # the shell's "command not found"; sil-run uses 0 to 3


@dataclass(frozen=True)
class Command:
    """A subcommand: the module whose `main` runs it, and what it does."""

    module: str
    summary: str
    leading: tuple[str, ...] = ()  # arguments put before the user's

    def run(self, path: str, arguments: list[str]) -> int:
        module = importlib.import_module(self.module)
        return module.main([*self.leading, *arguments]) or 0


@dataclass(frozen=True)
class Native:
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


@dataclass(frozen=True)
class Group:
    """Subcommands that belong together, under one name."""

    summary: str
    commands: dict[str, Command | Native | Group] = field(default_factory=dict)


ROOT = Group("SiL: deterministic Runs of virtual ECUs", {
    "check": Command("sil.check",
                     "run a Manifest twice and fail on any Recording difference"),
    "compare": Command("sil.compare",
                       "compare a Recording with a reference under a contract"),
    "footprint": Command("sil.footprint",
                         "report the worst-case payload memory a Manifest declares"),
    "run": Native("sil-run", "execute a Manifest into a Recording (the native sil-run)"),
    "bundle": Group("seal and run offline regression bundles", {
        "seal": Command("sil.bundle",
                        "record the identity of a bundle and its runtime",
                        ("seal",)),
        "verify": Command("sil.bundle",
                          "check every sealed identity without running",
                          ("verify",)),
        "run": Command("sil.bundle",
                       "verify a bundle, then execute every declared Run",
                       ("run",)),
        "matrix": Command("sil.matrix",
                          "run a list of sealed bundles as one CI job"),
    }),
    "fmi": Group("inspect FMUs and author Runs that use them", {
        "inspect": Command("sil.fmi.inspection",
                           "report whether this importer can drive an FMU"),
        "couple": Command("sil.fmi.coupling",
                          "author a Run of FMUs coupled through Channels"),
        "replay": Command("sil.fmi.authoring",
                          "author a Run that replays a Recording into one FMU"),
        "substitute": Command("sil.fmi.substitution",
                              "replace one coupled FMU with its Recording"),
    }),
    "recording": Group("prepare Recordings for replay", {
        "csv": Command("sil.csv_recording",
                       "convert CSV signals into a Recording"),
        "window": Command("sil.replay_window",
                          "select and rebase a window of a Recording"),
    }),
    "schema": Group("derive Schemas", {
        "import": Command("sil.schema_import",
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
