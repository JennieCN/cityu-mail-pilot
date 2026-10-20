#!/usr/bin/env python3
"""Offline evidence-gate prototype for RAG citations. Never imports the mail app.

This is **mechanical provenance checking, not entailment and not truth**. It can say:

* a cited URL is one of the retrieval candidates (or is not);
* a quoted sentence appears verbatim in the cited candidate passage (or does not);
* an asserted date occurs literally in the quote (or does not), and an explicitly
  asserted academic year is covered by source metadata (or remains uncertain);
* a passage contains text shaped like an instruction (prompt-injection suspicion).

It cannot say that a passage *entails* an answer, and it deliberately does not turn that
into a number: claims explicitly labelled as individual facts or guarantees
are returned as `needs_human_review`. Unlabelled claims are NOT semantically detected;
`supported` only means these mechanical checks passed, never "safe". No network, no model,
no service wiring.

Input contract (JSON files):

`--candidates`
    {"<source_id>": {"url": "https://...", "passages": ["..."],
                     "effective_from": "2025/26", "applicable_years": ["2025/26", "2026/27"]}, ...}

`--report`
    {"claims": [{"id": "c1", "text": "...",
                 "citations": [{"source_id": "regulations", "url": "https://...",
                                "quote": "verbatim sentence", "asserted_date": "2026-09-30",
                                "applicable_year": "2026/27",
                                "about_individual": false, "guarantee": false}]}]}

A declared `asserted_date`/`applicable_year` is validated whenever the key is present
and not `null`; a falsy value (`0`, `false`, `""`, `[]`, `{}`) is still a declaration and
fails closed instead of silently skipping the date/cohort checks.

Exit codes: **0** = the JSON was evaluated (by default this does *not* mean any claim
passed — inspect `claims[].status`/`counts`); **2** = malformed input (bad JSON, a
non-object, or an empty claims list); **3** = `--fail-on` was given and at least one claim
reached that status or worse. Consumers that gate on statuses must pass `--fail-on`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

#: Suspicion patterns, not a security boundary. A hit forces human review rather than
#: acting on the passage: retrieved text is evidence, never instructions.
INJECTION = (
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"disregard\s+(all\s+)?(previous|prior|above)",
    r"system\s*:",
    r"you\s+must\s+(now\s+)?(answer|output|reply)",
    r"do\s+not\s+(mention|reveal|disclose)",
    r"忽略(之前|以上|前面).{0,8}(指令|指示|要求)",
    r"你(必须|现在必须)(回答|输出|回复)",
    r"不要(提|说|透露|公开)",
)
INJECTION_RE = re.compile("|".join(INJECTION), re.I)

STATUS_RANK = {"supported": 0, "uncertain": 1, "needs_human_review": 2, "unsupported": 3}


class GateError(ValueError):
    """Malformed input, reported as a safe fixed diagnostic (never the input itself)."""


def year_of(text):
    match = re.search(r"(?:19|20)\d{2}", text or "")
    return int(match.group()) if match else None


def passage_has_injection(passages):
    return any(INJECTION_RE.search(p) for p in passages)


def valid_date_label(value):
    if not isinstance(value, str):
        return False
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            dt.date.fromisoformat(value)
            return True
        except ValueError:
            return False
    if re.fullmatch(r"\d{4}/\d{2}", value):
        return int(value[-2:]) == (int(value[:4]) + 1) % 100
    return False


def check_citation(citation, candidates):
    """Return (status, reasons, matched_passages). Mechanical checks only."""
    reasons = []
    if not isinstance(citation, dict):
        return "unsupported", ["unknown_source: citation is not an object"], []
    source_id = citation.get("source_id")
    if not isinstance(source_id, str):
        return "unsupported", ["unknown_source: not one of the retrieval candidates"], []
    candidate = candidates.get(source_id)
    if not isinstance(candidate, dict):
        return "unsupported", ["unknown_source: not one of the retrieval candidates"], []
    # Do not iterate dict keys or string characters as if they were passages.
    passages = candidate.get("passages", [])
    if not isinstance(passages, list) or any(not isinstance(p, str) for p in passages):
        raise GateError("candidate passages must be a list of strings")
    effective_from = candidate.get("effective_from")
    if effective_from is not None and not isinstance(effective_from, str):
        raise GateError("candidate effective_from must be a string or null")
    years = candidate.get("applicable_years")
    if years is not None and (not isinstance(years, list)
                              or any(not isinstance(y, str) for y in years)):
        raise GateError("candidate applicable_years must be a list of strings or null")
    url = citation.get("url")
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        valid_url = bool(parsed and parsed.scheme == "https" and parsed.hostname
                         and not parsed.username and not parsed.password)
    except ValueError:
        valid_url = False
    if not valid_url or url != candidate.get("url"):
        reasons.append("url_mismatch: cited URL is not the candidate URL")
    if passage_has_injection(passages):
        reasons.append("prompt_injection: passage contains instruction-shaped text; treated as data")
    quote = citation.get("quote")
    matched = []
    if not isinstance(quote, str) or not quote.strip():
        reasons.append("no_literal_evidence: citation quotes no passage text")
    else:
        matched = [p for p in passages if quote in p]
        if not matched:
            reasons.append("evidence_not_found: quoted sentence is not verbatim in the candidate passages")
        elif len(re.sub(r"\W", "", quote)) < 12:
            reasons.append("no_literal_evidence: quote is too short for this prototype; review required")
    # A declared date must not be silently skipped just because its value is falsy
    # (0, False, "", [], {}). `None` or an absent key means "no date was asserted";
    # anything else present is a declaration and has to survive validation.
    has_asserted = "asserted_date" in citation and citation["asserted_date"] is not None
    asserted = citation.get("asserted_date")
    if has_asserted:
        if not valid_date_label(asserted):
            reasons.append("invalid_date: expected a real ISO date or consecutive YYYY/YY academic-year label")
        else:
            # An unrelated date elsewhere in the same chunk is not cited evidence.
            text = quote if matched else ""
            if not re.search(r"(?<!\w)" + re.escape(asserted) + r"(?!\w)", text):
                reasons.append("date_not_in_evidence: the asserted date is not in the evidence")
            effective = year_of(candidate.get("effective_from") or "")
            claimed = year_of(str(asserted))
            if not effective:
                reasons.append("policy_scope_uncertain: effective-from metadata is missing or unknown")
            if effective and claimed and claimed < effective:
                reasons.append("policy_scope_uncertain: date precedes effective-from; chronology alone cannot prove staleness")
    # `applicable_year` is the same shape of bug: "" / 0 / False must not look like
    # "the caller did not map the date", which would quietly downgrade the check.
    applicable_present = "applicable_year" in citation and citation["applicable_year"] is not None
    applicable = citation.get("applicable_year")
    if has_asserted and candidate.get("applicable_years") and not applicable:
        reasons.append("policy_scope_uncertain: date-to-academic-year mapping was not supplied; do not infer term boundaries")
    if applicable_present:
        if not isinstance(applicable, str) or not applicable.strip():
            reasons.append("policy_scope_uncertain: asserted academic year must be a non-empty YYYY/YY label")
        else:
            years = candidate.get("applicable_years")
            if not isinstance(years, list) or applicable not in years or applicable == "unknown":
                reasons.append("policy_scope_uncertain: asserted academic year is not explicitly covered")
    if citation.get("about_individual") or citation.get("guarantee"):
        reasons.append("public_fact_limit: public pages cannot prove a fact about this individual")
    if any(r.startswith(("unknown_source", "url_mismatch", "evidence_not_found", "invalid_date"))
           for r in reasons):
        status = "unsupported"
    elif any(r.startswith(("no_literal_evidence", "prompt_injection", "public_fact_limit")) for r in reasons):
        status = "needs_human_review"
    elif reasons:
        status = "uncertain"
    else:
        status = "supported"
    return status, reasons, matched


def check_report(report, candidates):
    if not isinstance(report, dict):
        raise GateError("report must be a JSON object")
    if not isinstance(candidates, dict):
        raise GateError("candidates must be a JSON object keyed by source id")
    claims = report.get("claims")
    if not isinstance(claims, list) or not claims:
        raise GateError("report needs a non-empty claims list")
    checked = []
    for claim in claims:
        if not isinstance(claim, dict):
            checked.append({"id": None, "status": "unsupported",
                            "reasons": ["no_citation: claim is not an object"], "citations": []})
            continue
        citations = claim.get("citations")
        if not isinstance(citations, list) or not citations:
            checked.append({"id": claim.get("id"), "status": "unsupported",
                            "reasons": ["no_citation: claim cites no retrieval candidate"], "citations": []})
            continue
        results, worst = [], "supported"
        for citation in citations:
            if not isinstance(citation, dict):
                results.append({"source_id": None, "status": "unsupported",
                                "reasons": ["unknown_source: citation is not an object"]})
                worst = "unsupported"
                continue
            citation = dict(citation)
            for flag in ("about_individual", "guarantee"):
                citation[flag] = bool(citation.get(flag) or claim.get(flag))
            status, reasons, _ = check_citation(citation, candidates)
            if STATUS_RANK[status] > STATUS_RANK[worst]:
                worst = status
            results.append({"source_id": citation.get("source_id"), "status": status, "reasons": reasons})
        checked.append({"id": claim.get("id"), "status": worst,
                        "reasons": [reason for item in results for reason in item["reasons"]],
                        "citations": results})
    counts = {s: sum(1 for c in checked if c["status"] == s) for s in STATUS_RANK}
    return {
        "scope": "mechanical provenance only: URL, verbatim quote, literal date, injection shape",
        "semantic_entailment_checked": False,
        "thresholds_used": False,
        "note": "needs_human_review is a queue, not a pass; unsupported is not a claim that the answer is false",
        "claims": checked,
        "counts": counts,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--fail-on", choices=("none", "uncertain", "needs_human_review", "unsupported"),
                        default="none",
                        help="Exit 3 if any claim reaches this status or worse. Default 'none': "
                             "exit 0 only means the JSON was evaluated, not that claims passed.")
    args = parser.parse_args(argv)
    try:
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        candidates = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
        result = check_report(report, candidates)
    except (OSError, ValueError) as exc:
        detail = str(exc) if isinstance(exc, GateError) else type(exc).__name__
        print(json.dumps({"error": detail}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.fail_on != "none":
        threshold = STATUS_RANK[args.fail_on]
        if any(STATUS_RANK[c["status"]] >= threshold for c in result["claims"]):
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
