"""The standalone HTML report of a saved profile (#318).

`render_profile(profile_set)` writes one HTML file that opens offline from
`file://`, with no other file, no network request and no server. It uses
the page frame of the diagnostics report (sil.diagnostics_html.page): the
same style, theme switch, table sorting and filtering, and a policy that
allows only the page's own style and script.

Python writes the complete page, so its status, tables and caller tree need
no JavaScript. The filters of a CPU profile need report_assets/profile.js,
which reads one inert `application/json` block. That block is made by
`json_block`: JSON with every `<`, `>` and `&` and every character outside
ASCII (U+2028 and U+2029 too) as a `\\u` escape, so no report text can end
the block, open a comment or become markup. Virtual times in it are decimal
strings, so they stay exact.

The page embeds names, identities, counts and weights: never stack bytes,
source text, environment variables or Message payloads.
"""

from __future__ import annotations

import json

from sil.diagnostics_html import page
from sil.diagnostics_html_markup import Html
from sil.profile_html_cpu import cpu_body, explorer_data
from sil.profile_html_heap import heap_body
from sil.profile_set import ProfileSet

DATA_ID = "profile-data"


def json_block(value: object) -> Html:
    text = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    for character, escape in (("<", "\\u003c"), (">", "\\u003e"),
                              ("&", "\\u0026")):
        text = text.replace(character, escape)
    return Html(f'<script type="application/json" id="{DATA_ID}">{text}'
                "</script>")


def render_profile(profile: ProfileSet, source: str) -> str:
    """The page of a checked set; `source` is the text of profile.json."""
    if profile.report["capture"]["kind"] == "heap":
        return page("SiL heap profile", heap_body(profile), source,
                    styles=("report.css", "profile.css"),
                    scripts=("report.js",), download_name="profile.json")
    data = explorer_data(profile)
    return page("SiL CPU profile", cpu_body(profile), source,
                styles=("report.css", "profile.css"),
                scripts=("report.js", "profile.js"),
                download_name="profile.json",
                data=json_block(data) if data is not None else None)
