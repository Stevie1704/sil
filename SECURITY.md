# Security policy

## Reporting a vulnerability

Report a vulnerability through GitHub private security advisories:
<https://github.com/Stevie1704/sil/security/advisories/new>. The report stays
private to you and the maintainer until an advisory is published.

Do not open a public issue and do not open a pull request for a vulnerability.
A public report makes the problem exploitable before a fix exists.

## What a report should contain

A report is actionable when it contains:

- The affected artifact and version: the SiL release tag, the Python
  distribution version, or the container image digest.
- The affected component: kernel, Native participant ABI, Clock shim, Manifest
  builder or validator, Step protocol, FMI importer, Recording reader, or
  container image.
- The impact you claim, stated plainly. Example: a Manifest from an untrusted
  source executes arbitrary code outside the container; a Recording crashes the
  reader; a Run leaks a host path into a Recording.
- The smallest reproduction you have: a Manifest, a Participant, a Recording,
  and the exact command line. Attach files to the advisory rather than to a
  public location.
- The machine class you observed it on: OS, CPU architecture, and libc, or the
  container image digest.

Report the finding even when the reproduction is incomplete.

## What to expect

| Step | Expectation |
| --- | --- |
| Acknowledgement | Within 5 working days of the report. |
| Assessment | Within 15 working days: accepted with a severity, or declined with the reasoning. |
| Fix and advisory | Coordinated with you. The default embargo is 90 days from acknowledgement. |
| Credit | Your name or handle in the advisory, unless you ask otherwise. |

SiL is maintained by one person. These are honest targets, not a contractual
service level. If a report goes unacknowledged past the window above, send a
reminder through the same advisory thread.

Publish your findings after the advisory is public, or after the embargo ends,
whichever comes first. Tell the maintainer if you intend to publish earlier;
that is your right, and an early date can still be planned around.

## Supported versions

| Version | Receives fixes |
| --- | --- |
| The latest released version | Yes |
| Every earlier version | No |

SiL is below 1.0. A fix lands on `main` and ships in the next release. There
are no backports and no patch releases for earlier tags. Pin a release by tag
or image digest for reproducibility, and move the pin forward to take a fix.

## Scope

SiL runs the code a Manifest names. That is the whole purpose of the tool, so
the following are not vulnerabilities:

- A Participant, a Native participant shared library, or an FMU named by a
  Manifest that does anything the process is allowed to do. A Manifest is
  trusted input, exactly like a build script.
- A Run that exhausts memory or time because a Manifest declares route
  capacities or Arena slot counts that its Participants do not respect. Use
  `python -m sil.footprint` to see the declared worst case before a Run.
- A determinism violation. It is a correctness defect. Report it as a public
  issue.

These are in scope:

- Memory corruption in the kernel, the Clock shim, or the Recording reader that
  is reachable from a Manifest, a Recording, or Step-protocol traffic that a
  user did not author.
- An escape from the container image's non-root user, or any privilege the
  published image grants that the documented Run does not need.
- A secret, credential, or host path that a Recording, a Manifest hash, or a
  diagnostic message exposes without the caller putting it there.
- A published artifact whose content does not match the source revision it
  names.
