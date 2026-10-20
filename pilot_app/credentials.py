"""What a credential looks like, in exactly one place.

This used to live in `tools/handoff.py`, which is a development tool that reads
this project's own diaries and is therefore not published. That made the
publication tool depend on an unpublished file -- and the first time the export
was run from a clean tree, the exported copy could not even import itself. The
scanner is not a handoff concern anyway: it is the project's answer to "may this
text leave the machine", and both the handoff snapshot and the publication export
ask that question.

The one piece that *is* a handoff concern, the corpus of values the documentation
already publishes, is passed in by the caller (`corpus=`) instead of being
computed here: the corpus is built from files that only exist in the working
directory, and a module in `pilot_app/` must not reach for them.
"""

from __future__ import annotations

import os
import re

# (pattern, label, which capture group holds the secret, whether it applies to
# source files). Group 0 means the whole match is the secret; a named group means
# only the value is, so the check can ask "is *this value* public?" rather than
# "is this sentence public?".
SECRET_PATTERNS = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "疑似 API key（sk-…）", 0, True),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "疑似 AWS 凭据", 0, True),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥", 0, True),
    (re.compile(r"""(?i)\b(?:password|passwd)\s*[=:]\s*['"]([A-Za-z0-9!@#$%^&*_.+=-]{12,})['"]"""),
     "口令字面量", 1, True),
    # Unquoted form only. It is what a chat transcript or a log line looks like
    # ("password: hunter2"), but in source it matches ordinary code: the line
    # `password = secrets.decrypt(...)` yields the "secret" `secrets.decrypt`.
    (re.compile(r"""(?i)\b(?:password|passwd)\s*[=:]\s*([A-Za-z0-9!@#$%^&*_.+=-]{12,})"""),
     "口令字面量", 1, False),
)

# A value named like a stand-in is a stand-in -- the same reasoning as
# `_is_placeholder`, one step less strict. Without this the scan fires on every
# preview/test fixture in the tree, and an always-on warning is one nobody reads.
PLACEHOLDER_WORDS = ("preview", "example", "dummy", "fake", "placeholder",
                     "sample", "changeme", "replace-with", "your-", "fixture",
                     "long-enough", "todo", "xxxx")


def is_placeholder(value: str) -> bool:
    """True for a value that cannot be a credential.

    Two kinds qualify: one character repeated (the all-A master key this project
    uses in every command), and anything named like a stand-in. Recognising them
    is what keeps the scan from firing on each run, because a warning that is
    always on is one nobody reads -- the same lesson the Gmail staleness alert
    taught.
    """
    body = value.rstrip("=")
    if len(set(body)) <= 1:
        return True
    lowered = value.lower()
    return any(word in lowered for word in PLACEHOLDER_WORDS)


# Kept as an alias so the older name keeps working for callers that used it.
_is_placeholder = is_placeholder


def scan_secrets(text: str, *, source: bool = False, corpus: str = "") -> list[str]:
    """Refuse to write, or ship, something that carries a credential.

    A transcript or an archive is the kind of artifact that gets attached to a
    chat and leaves this machine, so it is where a key escapes to. The scan is
    deliberately crude -- but not so crude that it cries wolf: the all-A test key
    and the fixture passwords this project uses everywhere are ignored on purpose.

    `source=True` drops the unquoted patterns, which exist for prose and logs but
    in code match ordinary expressions (`password = secrets.decrypt(...)`).

    `corpus` is the text of everything this project already publishes. It is
    consulted only when `source=False`, and callers that scan *source* must not
    pass it, because the excuse is circular there: the corpus is built from files
    inside the same tree, so a key planted in a doc would be vouched for by the
    doc that contains it -- the gate opens exactly when it should close. Source
    scans lean on the fixture-naming rule instead.
    """
    if source:
        corpus = ""

    def candidate(value: str) -> bool:
        """True when this value is worth flagging: not a placeholder, not published."""
        return not is_placeholder(value) and not (value and value in corpus)

    findings = []
    for pattern, label, group, in_source in SECRET_PATTERNS:
        if source and not in_source:
            continue
        for match in pattern.finditer(text):
            if candidate(match.group(group)):
                findings.append(label)
                break
    for match in re.finditer(
            r"""(?:INFE_PILOT_MASTER_KEY|api_key|API_KEY)\s*[=:]\s*['"]?([A-Za-z0-9+/=_-]{16,})""",
            text):
        if candidate(match.group(1)):
            findings.append("非占位符形式的主密钥或 API key")
            break
    real = os.environ.get("INFE_PILOT_MASTER_KEY", "")
    if real and candidate(real) and real in text:
        findings.append("环境变量里的主密钥原值")
    return sorted(set(findings))
