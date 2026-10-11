"""`sil docs`: the user docs of this installed version, without network access.

The release build (`tools/release.py stamp-python`) copies the docs into the
package with a topic index, so they always match the installed version. A
source tree that was not stamped has no docs here; read `docs/` instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib.resources import files
from pathlib import Path

PROG = "sil docs"
EXIT_FAILURE = 1
EXIT_USAGE = 2
DOCS = Path(str(files("sil").joinpath("_docs")))


def topic_list(topics: dict[str, str]) -> str:
    width = max(map(len, topics))
    return "topics:\n" + "".join(f"  {topic:<{width}}  {summary}\n"
                                 for topic, summary in topics.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Print the user docs of this SiL version as Markdown. "
                    "Without a topic, list the topics.",
        allow_abbrev=False,
    )
    parser.add_argument("topic", nargs="?", help="the doc to print")
    parser.add_argument("--path", action="store_true",
                        help="print the directory of the docs, to search them")
    args = parser.parse_args(argv)
    try:
        topics = json.loads((DOCS / "topics.json").read_text())
    except OSError:
        sys.stderr.write(f"{PROG}: error: this installation has no docs; "
                         "a release wheel or the container image has them\n")
        return EXIT_FAILURE
    if args.path:
        print(DOCS)
    elif args.topic is None:
        sys.stdout.write(topic_list(topics))
    elif args.topic in topics:
        sys.stdout.write((DOCS / f"{args.topic}.md").read_text())
    else:
        sys.stderr.write(f"{topic_list(topics)}{PROG}: error: "
                         f"unknown topic '{args.topic}'\n")
        return EXIT_USAGE
    return 0


if __name__ == "__main__":
    sys.exit(main())
