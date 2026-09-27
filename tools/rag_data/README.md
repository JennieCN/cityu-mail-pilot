# Offline CityU corpus experiment

This is an operator-run research tool, **not a deployed RAG feature**. It does
not import `pilot_app`, connect to IMAP/SMTP, call an AI provider, read environment
credentials, modify the user database, or change report generation. The existing
mail service is unaffected. RAG project handoff remains **OneDrive HANDOFF yjc.md**.

## Reproduce (from repository root)

Use a new output filename every time; existing files are deliberately refused.
`/tmp` artifacts are disposable and are not a durable corpus deployment.

```bash
.venv-pilot/bin/python tools/rag_corpus.py build \
  --manifest tools/rag_data/sources.json --output /tmp/cityu-corpus-new.sqlite
# Above is a dry run, without network or file writes. Explicitly opt into fetch:
.venv-pilot/bin/python tools/rag_corpus.py build \
  --manifest tools/rag_data/sources.json --output /tmp/cityu-corpus-new.sqlite --fetch
.venv-pilot/bin/python tools/rag_corpus.py search \
  --corpus /tmp/cityu-corpus-new.sqlite --query 'late drop X grade transcript'
# Why a query returned nothing (terms, which matched, ranked ids):
.venv-pilot/bin/python tools/rag_corpus.py diagnose \
  --corpus /tmp/cityu-corpus-new.sqlite --query '退课之后成绩单会显示什么'
.venv-pilot/bin/python tools/rag_corpus.py evaluate \
  --corpus /tmp/cityu-corpus-new.sqlite --cases tools/rag_data/eval_cases.json
# Same evaluation with the reviewed bilingual expansion enabled:
.venv-pilot/bin/python tools/rag_corpus.py evaluate \
  --corpus /tmp/cityu-corpus-new.sqlite --cases tools/rag_data/eval_cases.json --glossary
# What a coverage floor would cost (recommends nothing; read the curve):
.venv-pilot/bin/python tools/rag_corpus.py calibrate \
  --corpus /tmp/cityu-corpus-new.sqlite --cases tools/rag_data/eval_cases.json --glossary
# Opt in to abstention for one run only:
.venv-pilot/bin/python tools/rag_corpus.py evaluate \
  --corpus /tmp/cityu-corpus-new.sqlite --cases tools/rag_data/eval_cases.json --glossary --min-coverage 0.5
# The set that is allowed to disagree with the one above (see "The held-out sets"):
.venv-pilot/bin/python tools/rag_corpus.py evaluate \
  --corpus /tmp/cityu-corpus-new.sqlite --cases tools/rag_data/heldout_cases.json --glossary
# And the one written by a machine that never read the glossary, on both settings:
.venv-pilot/bin/python tools/rag_corpus.py evaluate \
  --corpus /tmp/cityu-corpus-new.sqlite --cases tools/rag_data/heldout_independent_cases.json --glossary
# The second-machine holdout (55 cases) and the traditional A/B:
.venv-pilot/bin/python tools/rag_corpus.py evaluate \
  --corpus /tmp/cityu-corpus-new.sqlite --cases tools/rag_data/heldout_next_cases.json --glossary
# Page-level + passage-level evidence metrics (validates every quote first):
.venv-pilot/bin/python tools/rag_corpus.py evaluate-evidence \
  --corpus /tmp/cityu-corpus-new.sqlite --cases tools/rag_data/evidence_cases.json
# The stage-190 frozen challenge set (32 cases), page level and evidence level:
.venv-pilot/bin/python tools/rag_corpus.py evaluate \
  --corpus /tmp/cityu-corpus-new.sqlite --cases tools/rag_data/heldout_190_cases.json --glossary
.venv-pilot/bin/python tools/rag_corpus.py evaluate-evidence \
  --corpus /tmp/cityu-corpus-new.sqlite --cases tools/rag_data/heldout_190_evidence.json --glossary
# Reproduce the stage-190 chunk-size A/B: build a second corpus at another granularity
# (default 1200, 200 overlap) and run the same evaluations above on it.
.venv-pilot/bin/python tools/rag_corpus.py build \
  --manifest tools/rag_data/sources.json --output /tmp/cityu-corpus-cs1600.sqlite --fetch --chunk-size 1600
# Offline citation gate prototype (synthetic report + candidate passages, no service):
.venv-pilot/bin/python tools/rag_evidence_gate.py --report report.json --candidates candidates.json
# Gate exit contract: 0 = evaluated, 2 = malformed, 3 = --fail-on tripped.
.venv-pilot/bin/python tools/rag_evidence_gate.py --report report.json --candidates candidates.json --fail-on unsupported
.venv-pilot/bin/python -m unittest pilot_app.tests.test_rag_offline -v
```

Snapshot content fingerprints (sha256 of each page's extracted text, stable across
fetches; the SQLite file hash is only a build id): `faq f2d1f35e…`, `registration
92c23946…`, `regulations 9d0c6f52…`, `restrictions 95d8e455…`, `schedule 829f1419…`.
That regulations hash is the **e7c9db0 preserve-superscript** extractor (`[sup:N]`); the
`a8edef3` footnote-drop build was `fa9e706f…` and the earlier glued-number bug was
`2b73adb6…` (both recoverable from git; this line was left stale by e7c9db0 and fixed in
stage 190). The evidence document pins all five page hashes plus a `chunk_ordinal` per
quote; any extractor, page, or chunking change makes `evaluate-evidence` refuse to score
rather than silently grade a different snapshot.

Only use public/synthetic queries on the CLI; command arguments may be retained
by the shell. No database binary or captured university HTML is committed.
The command checks for FTS5 when creating the index; a missing extension fails
instead of silently changing the search algorithm. No new package is installed.

## Source selection and collection contract

Five reviewed public ARRO HTML pages form a deliberately narrow starting corpus:
registration, undergraduate regulations, class schedule, restrictions, FAQ.
`sources.json` records audience and applicability caveats, not a claim that every
sentence is current or usable for every entering cohort. **Review date is not a
policy publication/effective date.** Regulations contain multiple versions.
The tool does not recursively follow links, import PDFs, fetch personal AIMS
data, or evaluate JavaScript. Some key dates are in linked XLSX/JSON, so the HTML
corpus cannot answer all date questions. No automatic full-site crawl is enabled.

Collection is sequential and bounded (50 sources, 2 MB per page). Exact host
allowlist, HTTPS, no credentials/query/fragment, no redirects, no environment
proxies, public DNS addresses pinned to each connection, normal TLS hostname
verification. Each request runs in a subprocess with a 30-second total deadline
(DNS and headers included); there is also a socket timeout and body budget.
Redirects or missing `<main>` require manual source/extractor review, never a
silent fallback to the whole page. Robots is fetched first; unavailable or
unsupported policies fail closed. Wildcard/Allow rules are deliberately refused
because stdlib robotparser does not implement their full semantics. Observe
crawl delay/request rate, generic noindex/none metadata and repeated headers.
Robots permission does not by itself settle copyright or terms of use.

HTML extraction removes global navigation, scripts, forms, hidden attributes and
inline hidden styles; comments are not indexed. Explicit `role="doc-noteref"`
references are omitted; unlabelled `<sup>`/`<sub>` contents are preserved as
`[sup:...]` / `[sub:...]`, never joined into numbers or silently deleted.
For example `31<sup>1</sup>` is not `311`, and `x<sup>2</sup>` does not become `x`.
This supersedes the DS blanket-drop extractor. It is **not a browser/CSS engine**:
class-based hidden sections and repeated in-page enquiries may remain. Extracted
text is untrusted evidence, never instructions. Integration will need prompt
injection tests before any model receives these passages.

## Storage and retrieval contract

**2026-09-26 independent review:** the historical tables below belong to DS commit
`544d38c` and its frozen snapshots, not the corrected superscript-preserving extractor.
Rebuilding a corpus can change source content hashes and evidence quotes; the existing
evidence manifest must fail closed on mismatches. Do not silently update its expected
answers or reuse old scores as current measurements. Re-annotate a new version and
rerun before advancing to integration. This review adds no model or production wiring.
Precedents reviewed: [Trafilatura](https://github.com/adbar/trafilatura) (Apache-2.0;
reference only, full extraction dependency stack not introduced) and
[W3C doc-noteref](https://www.w3.org/TR/dpub-aria-1.0/#doc-noteref). No code copied.

Separate SQLite database with application ID/schema version. Sources carry URL,
title, audience, scope note, review date, fetch timestamp and cleaned-content
SHA-256. Overlapping character chunks and pre-tokenized FTS5 fields use BM25.
NFKC normalization plus Latin tokens/CJK bigrams avoids mixed-script token gluing;
FTS expressions are generated from safe tokens, not interpreted user syntax.
There is **no machine translation, no simplified/traditional mapping, no stemming,
no embedding model, no reranker and no answer generator**.

An **optional reviewed bilingual glossary** (`glossary.json`) maps a small set of
Chinese domain terms to English terms that the collected pages actually use. It is
off unless `--glossary` is passed, so the baseline stays reproducible and no
retrieval behaviour changes silently. It is also the only Chinese segmenter in the
tool: without a listed term, Chinese falls back to sliding bigrams
(`退课之后成绩单` becomes 退课/课之/之后/后成/成绩/绩单), and bigrams cannot match an
English corpus. A reviewed term **replaces** its span rather than joining it, so the
bigrams of `成绩单` are dropped once `transcript` is added. Measured: dropping them
changes no retrieval result at all (0/30), because a term that matches nothing never
contributes to BM25; the point is that otherwise they make every coverage figure
meaningless.

Build to a temporary file, verify SQLite/FTS integrity, publish with an exclusive
hard link: failures leave no target and cannot overwrite existing DBs. Retrieval
uses `mode=ro` and `query_only`, rejects other databases, returns at most 10 source
candidates and never stores query history. Snapshots over 30 days old (default)
or fetched in the future are excluded; recency is not policy validity. All hits
are labelled `official_snapshot` and `applicability_unverified`. BM25 rank is not
a confidence score. A nonempty result is **not proof the question is answerable**.

**Abstention is opt-in and has no default.** `--min-coverage T` refuses a query whose
idf coverage is below `T`; coverage is the share of the query's inverse-document-frequency
mass the corpus actually contains, with a term the corpus has **never** seen weighted as
the rarest possible (a `df=0 -> 0` weighting hides exactly the signal you want: it once
reported a 2-of-5 match as full coverage). Coverage ignores a script the corpus does not
use at all — on a snapshot with no CJK, a Chinese bigram is an artefact of not having a
segmenter and is not evidence either way; add one Chinese source and that stops being true
automatically. `calibrate` sweeps the threshold on a case set and prints what each one
costs; **it deliberately recommends nothing.**

## Measured effect of the glossary (2026-09-26, five pages / 30 cases)

Same corpus, same cases, the only difference being `--glossary`:

| | hit@1 | hit@3 | Chinese hit@3 | English | mixed | negative false hits |
|---|---|---|---|---|---|---|
| off | 0.654 | 0.692 | **0.0** (all eight returned `[]`) | 1.0 | 1.0 | 2/4 |
| on | **0.846** | **1.000** | **1.0** | 1.0 | 1.0 | 2/4 |

Before this, every Chinese case returned an empty set, not a low rank: 11 bigrams and
zero of them occurring in an English page. `diagnose` exists to keep those two
failures distinguishable — a vocabulary gap versus a ranking miss.

**This is not evidence of generalisation.** The 30 cases are the development smoke
set and were visible while the glossary was written, which is exactly the tuning the
section above warns against. Treat `1.000` as "the expansion fires on the vocabulary
it was reviewed against", nothing more. The negatives are unchanged at 2/4, so
abstention is untouched here: the glossary did not make the tool worse at refusing,
but it did not help either.

## The held-out sets, and what they say (2026-09-26)

`heldout_cases.json` — **33** questions (14 Chinese, 13 English, 2 mixed, 4
negatives; the earlier "32" in this file and in the commit message was an arithmetic
slip — the parenthetical sums to 33 and the file has 33), written **from the page text**
after the glossary was committed, and deliberately using Chinese vocabulary the glossary
was not built around (时间票, 候补名单, 上课时间, 星期六, 学院, 学费, 双主修, 通识课程 …):

| set | glossary | hit@1 | hit@3 | Chinese hit@3 | English | mixed | negative false hits |
|---|---|---|---|---|---|---|---|
| dev (30) | off | 0.654 | 0.692 | 0.0 | 1.0 | 1.0 | 2/4 |
| dev (30) | on | 0.846 | 1.000 | 1.00 | 1.0 | 1.0 | 2/4 |
| **held-out (33)** | off | 0.414 | 0.517 | 0.07 | 0.92 | 1.0 | 2/4 |
| **held-out (33)** | **on** | **0.552** | **0.759** | **0.57** | 0.92 | 1.0 | 2/4 |

**The 1.000 does not survive.** The glossary still multiplies Chinese retrieval (0.07
-> 0.57), but six of fourteen held-out Chinese questions return **an empty set**, not a
low rank, and every one of them fails for the same reason: its content words are not in
the glossary (时间票, 候补名单, 上课时间, 星期六, 商学院, 学费). That is the same vocabulary
gap as before, just sampled outside the set the glossary was reviewed against. Two
consequences worth stating plainly:

* The glossary is a **bounded, hand-curated** lever. It generalises to the vocabulary it
  covers and to nothing else; the honest number to quote for it is **0.57**, not 1.00.
  Making it bigger is manual review work with diminishing returns, and every addition
  re-invalidates the table above.
* The held-out negatives are **different instances of the same two classes**
  (`my friend's student number and home address`, `2027 tuition fee for the new
  curriculum`) and land at 2/4 again — the abstention finding is not an artefact of the
  development set's particular negatives.

Provenance limit, stated rather than implied: the glossary was committed (`40b1f7c`)
before these questions were written, and `test_the_reviewed_glossary_is_frozen` pins its
SHA-256 so any later edit invalidates the numbers — but **the same author wrote both**,
so this set catches tuning to the 30 development queries, not tuning to the glossary
itself.

**None of the first three sets is a true holdout any more.** The 30 development cases,
the 33 page-derived cases and the 49 second-machine cases have all been read and used to
choose the glossary and the traditional variants, so they are now
**development-visible**: their numbers are a record of a decision already made, not
evidence for the next one. Only `heldout_next_cases.json` (55 cases) was authored after
those decisions; as soon as it informs a change it joins them, and the next decision
needs a newly and independently authored set.

## The independent set: written by a machine that never read the glossary

`heldout_independent_cases.json` — **49** questions (21 Chinese of which **9 traditional**,
23 English, 5 mixed, 5 unanswerable) produced on the second machine, which was given the
five page URLs and **explicitly forbidden from reading `glossary.json`,
`eval_cases.json` or `heldout_cases.json`**. It also found, without being told to, that
Hong Kong students write their questions with English acronyms mixed in
(`AIMS`, `DegreeWorks`, `CGPA`, `CRN`) and in Cantonese (`課堂係咪逢整點開始？`).

| set | glossary | hit@1 | hit@3 | Chinese hit@3 | traditional hit@3 | English | mixed | negative false hits |
|---|---|---|---|---|---|---|---|---|
| **independent (49)** | off | 0.727 | 0.773 | 0.53 | 0.62 | 0.95 | 1.00 | **4/5** |
| **independent (49)** | **on** | **0.795** | **0.864** | **0.74** | **0.62** | 0.95 | 1.00 | **5/5** |

Three things this set says that the first two could not:

1. **Chinese is 0.74 here, not 0.57** — and 0.53 *with the glossary off*. Both numbers
   are driven by the same thing: these questions carry Latin tokens (`CGPA`, `CRN`,
   `minor`, `sem`) that an English corpus matches directly. So the honest headline is not
   "0.57" or "0.74" but **"it depends how much English the asker mixes in"**, and the
   earlier 0.07 was an artefact of a set written in pure Chinese.
2. **The glossary does nothing for traditional Chinese: 0.62 -> 0.62.** It is
   simplified-only. The 0.62 comes entirely from the embedded Latin tokens. Adding
   traditional variants is mechanical work that has not been done, and until it is, a
   Hong Kong student writing in traditional characters gets no benefit from it.
3. **The glossary costs precision: negative false hits go 4/5 -> 5/5.** Expanding a
   Chinese question into English terms pulls pages into scope that the raw query did not
   reach. This is the same trade the calibration curve shows, now with a price tag on the
   recall side rather than the abstention side.

## Traditional variants: measured, and the null result (2026-09-26)

The glossary was simplified-only, so traditional questions got nothing. `glossary.json`
gained a hand-reviewed `traditional_variants` map (27 entries) that resolves a
traditional surface form to an **existing** reviewed term — no new English vocabulary,
no source rewriting, query only. Candidates were cross-checked against OpenCC's
`STCharacters`/`TSCharacters`; ambiguous characters were decided by hand (`注册`→`註冊`
not `注册`, `课表`→`課表` not `課錶`, `先修`/`限制`/`考核`/`批准` unchanged).

A/B on one frozen snapshot, old glossary `44fd944c…` vs new `e7121eb6…`:

| set | glossary arm | hit@1 | hit@3 | Simplified zh | Traditional zht | negatives (false candidates) |
|---|---|---|---|---|---|---|
| dev (30) | simplified only / + traditional | 0.846 | 1.000 | 8/8 | n/a | 2/4 |
| held-out (33) | simplified only / + traditional | 0.552 | 0.759 | 8/14 | n/a | 2/4 |
| independent (49) | simplified only / + traditional | 0.795 | 0.864 | 9/11 | 5/8 | 5/5 |
| **next (55)** | **simplified only** | **0.771** | **0.857** | **9/11** | **7/9** | **18/20** |
| **next (55)** | **+ traditional** | **0.771** | **0.857** | **9/11** | **7/9** | **20/20** |

**The traditional variants do not move a single hit rate on any frozen set, and they
cost two false candidates on the second-machine holdout.** They fire (4 of the 9
traditional positives change some ranking; `next-zht14`/`15` go from no candidate to
candidates because `課表` expands to `class schedule`), but the expected sources were
already in the top 3 through embedded Latin tokens. The two traditional positives that
still fail do so for a vocabulary reason, not a spelling one: `大學的課堂時間…` uses
課堂/整點 and `…不打算註冊任何科目…` expands to `registration` while the set expects
`regulations`. So the earlier "cheapest uncovered win" guess is **not supported by this
measurement**: mapping existing domain terms across scripts is correct but invisible
here, and the real gap is Cantonese/domain vocabulary (`課堂`, `時間`, `宿舍`, `主修`)
that was never reviewed. Treat the traditional map as a bounded correctness fix that
stays off by default, not as a recall win.

## The second-machine holdout (55 cases, 2026-09-26)

`heldout_next_cases.json` — **55** questions (35 answerable, 20 not; 15 simplified
Chinese, **15 traditional**, 9 Cantonese/mixed, 16 English) produced on the second
machine at commit `f55f88cd` (the pre-fix extractor), which was given the page URLs and
explicitly forbidden from reading any glossary or existing case file. It carries a
verbatim evidence quote per covered source and a `counter_evidence` quote for 12 of the
20 unanswerable cases (e.g. the registration page's `Key Dates` block extracts as labels
only). All **77 quotes were re-checked verbatim against the corrected snapshot**;
`next-*` ids are disjoint from the other sets. It is the set allowed to disagree: on it
the simplified glossary raises zh 7/11 → 9/11 while false candidates rise 17/20 → 18/20;
the traditional variants then add nothing and take it to 20/20.

## Evidence-level data and metrics (2026-09-26)

Page hit rates overstate answerability: the right page is not the right passage.
`evidence_cases.json` — **34** cases (27 answerable, 7 not) add, per answerable case,
one or more verbatim quotes from the fixed snapshot plus the exact official URL, the
source content hash, an `applicable_year` (or `unknown`) and a `caveat`. Unanswerable
cases carry **no** evidence and a stated reason; a retrieved candidate for them is a
`false_candidate`, explicitly **not** a generation hallucination because this tool has no
generator. Coverage: simplified, traditional, Cantonese/mixed, personal facts,
eligibility guarantees, future deadlines, superseded policy, and two cohort versions on
the regulations page (`2019/20 or before` vs `2020/21 and thereafter`) proven by
different sentences so the versions cannot be mixed.

`evaluate-evidence` validates every quote/URL/hash against the corpus before scoring,
then reports page-level and passage-level hits separately (`evidence_all@K` = every
required quote appears in the top-K passages):

| glossary | page@1 | page@3 | evidence@3 | evidence@10 | unanswerable with a candidate |
|---|---|---|---|---|---|
| off | 0.481 | 0.519 | **0.296** | 0.519 | 4/7 |
| on | 0.593 | 0.815 | **0.370** | 0.593 | 6/7 |

At K=3 the page-level number roughly **halves** when the quoted passage has to be
retrieved: `ev-zh01` (credit requirement), `ev-zh04` (second-major CGPA), `ev-zht02`
(traditional second-major) and `ev-mx03` (late drop) all return the right page with the
wrong passage. That is the concrete case for citation validation at answer time; it is also
why these are `source_hit`/`evidence_hit`, never Recall. (These numbers were re-measured
after the stage-190 re-annotation; the old 0.259/0.333 came from a mis-paired cohort quote,
see §B below.)

## Offline evidence gate prototype (2026-09-26)

`tools/rag_evidence_gate.py` checks a report's citations against retrieval candidates:
cited URL == candidate URL, quoted sentence verbatim in a candidate passage, asserted
date present in the quoted evidence (not elsewhere in a chunk), explicit academic year
covered by metadata, and instruction-shaped text in a passage flagged as prompt-injection
suspicion. It is **mechanical provenance only**: output states
`semantic_entailment_checked: false` and `thresholds_used: false`, and anything needing
meaning is not automatically recognized: only explicitly labelled personal facts and
guarantees go to `needs_human_review`. `supported` is never semantic approval.
An earlier date than effective-from flags uncertainty, not proof of stale policy.
Synthetic fixtures cover wrong URL, non-verbatim quote, no-evidence
date, stale policy, personal fact, and an injected passage whose text is never executed.
No network, no model, no service wiring; candidates are a JSON file produced by the
retriever.

The CLI's exit 0 means JSON was evaluated, **not** that claims passed; consumers
must inspect statuses/counts. Pass `--fail-on {uncertain,needs_human_review,unsupported}`
to turn a status floor into exit **3**; malformed input (bad JSON, a non-object, an empty
claims list) now exits **2** with a fixed diagnostic instead of a traceback. A declared
`asserted_date`/`applicable_year` is validated whenever the key is present and not `null`:
a falsy value (`0`, `false`, `""`, `[]`, `{}`) is a declaration and fails closed rather
than silently skipping the date/cohort check. Dates must be valid ISO calendar dates or consecutive
`YYYY/YY` labels. A calendar date does not automatically identify a cohort or academic
year: when source year restrictions exist, supply `applicable_year` explicitly or
receive `uncertain`. Missing effective-from metadata also leaves date checks uncertain.
Missing/non-HTTPS URLs fail closed. Tiny quotes (under 12 alphanumeric characters)
require review; this is a conservative heuristic, not a correctness score.

## Abstention: what the numbers allow and what they forbid (measured 2026-09-26)

`calibrate --glossary` on the same five pages and 30 cases. Weakest positive coverage
**0.4766** (`en16`, "course registration waiting list"); strongest false-hit coverage
**0.7058** (`none03`, "my personal examination seat number tomorrow"):

| `--min-coverage` | positives refused | false hits removed |
|---|---|---|
| 0.5 | `en16` | `none04` |
| 0.7 | `en03`, `en16` | `none04` |
| 0.75 | `en03`, `en16` | `none03`, `none04` |

Two conclusions, and the second one is the important one:

1. **The two negative classes are not the same problem.** `none01`/`none02` (off-topic) are
   already refused at coverage 0 with the gate off — they match nothing. `none04`
   ("2029 scholarship guaranteed eligibility deadline") is mostly words the corpus has
   never used, coverage 0.309, and the gate can remove it.
2. **`none03` cannot be refused at retrieval.** It is *topically on point* — examination,
   number, regulations all occur — and its coverage (0.706) is **higher than the weakest
   legitimate question (0.477)**. Any threshold that rejects it also rejects two real
   questions. This is not a tuning failure to be fixed with a better constant: a lexical
   scorer cannot know that "my personal seat number" is a personal fact absent from
   public pages, because nothing in the query is out-of-vocabulary. **The honest place to
   refuse that class is citation validation at answer time** — the answer step must be
   able to say "these passages do not contain this" — not the retriever.

So the gate stays **off by default and recommends no threshold**. Enabling it buys
precision with a true positive, and which side of that trade is acceptable is a product
decision that needs the held-out set, not this smoke set.

## Evaluation interpretation

30 synthetic questions (16 English, 8 Chinese, 2 mixed, 4 negatives). No real
emails or personal data. `expected` contains **alternative acceptable source IDs**,
not an exhaustively annotated set of relevant passages. Thus metrics are named
`source_hit_rate_at_1/3`, not Recall@K. They test finding a source, not finding the
right passage or producing a correct recommendation. Negatives deliberately
include a personal seat question and unsupported future scholarship dates.

This small hand-authored set is a development smoke benchmark, not an independent
holdout. Do not tune synonyms to it then claim general accuracy. Add separately
reviewed passage/effective-year labels and held-out questions before deployment.
`peak_process_rss_bytes` is the CLI process high-water RSS, **not incremental
server memory**. The 30-query p95 on five pages is not a scale/load benchmark.
The report includes a corpus SHA-256, but it identifies a **build**, not a content
snapshot: the database stores each source's fetch time, so two builds of the same pages
give different hashes with identical byte size and identical readings (measured twice on
the second machine: `e4481378…` vs `3568da38…`, and twice on this one: `a4db98d0…` vs
`fd6b6be5…`, all four at 401 408 bytes). Never use it to claim two runs share a corpus.
No artificial pass gate turns a failed retrieval benchmark into a production approval.

## Stage 190: rebuild, re-annotation, challenge set, and the passage A/B (2026-09-26)

Everything below is offline and still **NO-GO**. It fixes a stale record, adds one frozen
comparison set, measures one retrieval-unit A/B, and tightens the gate's input contract.

### A. Freeze and rebuild: the extractor changed, the website did not

The five pages were re-fetched once on `2026-09-26T11:56:56Z` under the existing bounded
rules (sequential, host allowlist, 2 MB/page, subprocess deadline) and the raw HTML was kept
only under `/tmp` for the comparison. Per-page `content_sha256` of the extracted text:

| page | sha256 | in evidence doc |
|---|---|---|
| registration | `92c23946…` | yes |
| regulations | `9d0c6f52…` | yes |
| schedule | `829f1419…` | yes |
| restrictions | `95d8e455…` | yes |
| faq | `f2d1f35e…` | yes |

Four hashes are byte-identical to the previously recorded snapshot. `regulations` is not,
and the cause is **the extractor, not the site**: running the `544d38c` extractor on the
same freshly fetched HTML reproduces the previously pinned `fa9e706f…` exactly. The only
difference between the two extractions is 14 inserted `[sup:N]` markers (`31 [sup:1]`,
`2.00 [sup:5]`, …) plus surrounding whitespace — no deletions or substitutions. So `e7c9db0`
changed the text and the site did not, but that commit left the README hash line and the
evidence snapshot pointing at the `544d38c` build. That is why `evaluate-evidence` refused
to score the rebuilt corpus (`exit 2`, `snapshot url/hash mismatch for regulations`) before
this stage re-annotated it; both records are now consistent, and the old build is still
recoverable from git.

Original-text check on the five pages: regulations has 15 `<sup>` tags and 14 markers in the
text (one superscript lies outside the extracted `<main>`); no page uses `<sub>`
or `role="doc-noteref"`, and hidden content appears only as `aria-hidden="true"` (2–3 per
page) and `display:none` (faq, 2). The `H₂O`/`x²` and `doc-noteref` branches are therefore
covered by unit tests, not by these pages. Date/cohort strings actually present include
`2019/20 and before`, `2020/21 and thereafter`, `2024/25 and before`, `2025/26 and
thereafter`.

### B. Evidence re-annotation (34 cases) and the independent re-check

Every case was re-validated against the new snapshot. All quotes are still verbatim
(0 changed); regulations' snapshot hash was updated to `9d0c6f52…`; each quote carries a
`chunk_ordinal` pinning it to the 1200-char build chunk that contains it, and
`evaluate-evidence` now **fails closed** if that pinned chunk does not contain the quote
(which is also why the granularity A/B below used a copy of the document with the pin
removed). `applicable_year` was re-derived from the quotes themselves: three cases whose
quotes did **not** name the asserted cohort/version (`ev-zh03`, `ev-zh04`, `ev-zht02`) were
downgraded to `unknown`, and `ev-zh20` was re-pointed at the sentence that actually names
the `2020/21 and thereafter` cohort (`The minimum CGPA requirement mentioned in AR16.3 and
AR16.4 only applies to students admitted in 2020/21 and thereafter`). The old document is
recoverable from commit `a1dc195`; `provenance` inside `evidence_cases.json` records the
mapping. The second machine independently re-checked the **pre-fix** document against the
frozen raw HTML (`job-0926-202225-78`, exit 0): 5/5 raw page hashes matched, 29/29 quotes
were verbatim, **5 `applicable_year` labels were not literal in their own quotes**, and the
regulations snapshot hash was stale — the same three findings this section fixes. After the
re-annotation the same mechanical check reports 0 year mismatches on this side. Details in
the stage-190 round entry in `HANDOVER.md`.

### C. Baseline on the rebuilt snapshot (no ranking change)

The rebuilt corpus reproduces every previously recorded number byte-for-byte, on Mac
(Python 3.9 / SQLite 3.51) and on the second machine (Python 3.14.4 / SQLite 3.46.1). The
one evidence-level exception is deliberate: after the §B re-annotation (a mis-paired cohort
quote was replaced) `evidence@3` reads 0.296/0.370 instead of the old 0.259/0.333 — the
corpus is the same, the annotation is not.

| set | glossary | hit@1 | hit@3 | negatives | Chinese | traditional |
|---|---|---|---|---|---|---|
| eval (30) | off / on | 0.654 / **0.846** | 0.692 / **1.000** | 2 / 2 | 0.0 / 1.0 | n/a |
| held-out (33) | off / on | 0.414 / 0.552 | 0.517 / 0.759 | 2 / 2 | 0.07 / 0.57 | n/a |
| independent (49) | off / on | 0.727 / 0.795 | 0.773 / 0.864 | 4 / 5 | 0.53 / 0.74 | 0.62 / 0.62 |
| next (55) | off / on | 0.743 / 0.771 | 0.800 / 0.857 | 17 / 20 | 0.64 / 0.82 | 0.78 / 0.78 |
| evidence (34) | off / on | page@3 0.519 / 0.815 | ev@3 0.296 / 0.370 | 4 / 6 false candidates | | |

### D. The passage A/B: chunk granularity (default unchanged)

Hypothesis: part of the evidence@3 gap is the retrieval unit, so a different chunk size
might let the answering sentence rank without touching the model. This is not a
constant-context-budget comparison: fixed k with larger chunks allows more text. `build`
gained an explicit `--chunk-size` option (default **1200** characters, 200 overlap, i.e. the
baseline). Only granularity changes; query, `k`, glossary and scoring do not, and `k` is
never raised. Measurements are on the visible sets and the frozen challenge set of §E:

| chunk | ev@3 off | ev@3 on | ev@10 on | page@3 on | next@3 off | next@3 on | challenge page@3 on | challenge ev@3 on |
|---|---|---|---|---|---|---|---|---|
| 400 | 0.259 | 0.333 | 0.519 | 0.778 | 0.829 | 0.914 | 0.826 | 0.478 |
| 800 | 0.407 | 0.444 | 0.519 | 0.815 | 0.771 | 0.857 | 0.783 | 0.522 |
| **1200 (default)** | 0.296 | 0.370 | 0.593 | 0.815 | 0.800 | 0.857 | **0.870** | **0.609** |
| 1600 | 0.296 | 0.407 | 0.667 | 0.815 | 0.800 | 0.857 | 0.870 | 0.565 |
| 2400 | 0.296 | 0.519 | 0.667 | 0.741 | 0.829 | 0.886 | 0.783 | 0.652 |

**No size dominates, so the default stays 1200 and no new scheme is adopted.** The visible
sets alone would tempt a change (1600 raises ev@3 and ev@10 and keeps page@3; 2400 raises
ev@3 further), but even ignoring that the gain is partly a budget effect — a bigger chunk
gives each candidate more text — the **frozen challenge set rejects 1600** (evidence@3
0.565 vs 0.609) and so does page@3 at 2400 (0.741). Smaller chunks are no better: 800 beats
1200 on visible evidence@3 but loses evidence@10 (0.519), the page-level `next` number
(0.771 off) and the challenge set (0.522); per language the directions disagree (800: zht up
but en down; 1600: mixed up, zht down). This is a null result, not a small win.

One possible explanation: for the 29 required evidence quotes, the chunk containing
the quote shares on average only **12.1 %** of the query's terms; 27 of 29 share under a
third, and 22 of those already have the right page in the top 3. The answering sentence does
not use many of the asker's words. This does not prove a ceiling for all lexical methods
or establish that embeddings would improve the result. The
honest places to close that gap remain citation validation at answer time and, as a separate
arm, embeddings.

### E. Frozen challenge set from the second machine (32 cases)

`heldout_190_cases.json` (page level) and `heldout_190_evidence.json` (the same cases with
the snapshot header): **32** questions written on the second machine (Linux/Python 3.14.4)
from the five frozen pages only, with `glossary.json`, the existing case files and the
retriever explicitly off-limits, and frozen before evaluation on this challenge set.
Visible-set A/B files already existed before the challenge artifact was generated;
do not describe the challenge set as preceding every experiment. Eight each in English,
simplified, traditional and mixed; 23 answerable, 9 unanswerable; every answerable quote was
validated verbatim against the snapshot (0 mismatches). The generated file is pinned by
SHA-256 in `test_rag_offline`, and this is the only frozen comparison for stage 190 — it
becomes development-visible as soon as it informs a change. On it the baseline reads page@3
0.783/0.870 and evidence@3 0.609/0.609 (glossary off/on); §D is the comparison.

### F. Gate input contract

`rag_evidence_gate` now validates a declared `asserted_date`/`applicable_year` whenever the
key is present and not `null` (`0`, `false`, `""`, `[]`, `{}` fail closed instead of
skipping the check), and a citation/claim of the wrong JSON type is `unsupported` instead of
raising. The CLI keeps the old contract (`exit 0` = JSON evaluated, **not** claims passed)
and adds `--fail-on {none,uncertain,needs_human_review,unsupported}` (exit 3); malformed
input exits 2. `supported` still means mechanical checks only, never entailment.

### G. Cross-machine

Both machines ran the RAG tests OK and reproduced every evaluation number above
identically, including `evaluate-evidence` with the pinned `chunk_ordinal`. The
`heldout_190` set was also generated on the second machine and its recorded per-page content
hashes match this machine byte-for-byte.

## Codex acceptance follow-up (2026-09-26)

The stage-190 measurements above were independently reproduced against its saved corpus.
This is partial acceptance, not a production gate: constant text-budget A/B and generated
answer evaluation remain unperformed. All 20 negatives in `heldout_next` and all 9 in the
new challenge set still retrieve candidates with the glossary on. No default changed.

Candidate-field validation was also incomplete: a `passages` object could be iterated as
keys and incorrectly pass provenance checks; null passages or numeric effective metadata
could crash. The gate now rejects malformed passages/effective_from/applicable_years with
a fixed GateError (CLI exit 2), without echoing candidate text. Regression cases cover
these nested shapes. Only cited candidates are inspected; this is not a general JSON
Schema validator. [python-jsonschema/jsonschema](https://github.com/python-jsonschema/jsonschema)
was reference-only for explicit type validation; no code copied or dependency installed.

## Open-source review

- [SQLite FTS5](https://www.sqlite.org/fts5.html): actual reused capability (Python's
  existing SQLite runtime); BM25, FTS and query syntax reference.
- [beir-cellar/beir](https://github.com/beir-cellar/beir) (Apache-2.0): reviewed for the
  stage-190 passage A/B. Reused as a **reference for the harness shape only** — separate
  corpus/query/qrels files and reporting several metrics instead of one — **no code
  copied, no model or dependency stack introduced**. Its datasets and retrievers were not
  downloaded or run; this tool stays zero-dependency and offline.
- [simonw/sqlite-utils](https://github.com/simonw/sqlite-utils) (Apache-2.0): reviewed
  README, license, tests/test_fts.py and maintenance notes. Its FTS tests and
  relevance-ordering design informed the checks; **no code copied and no new
  dependency**. Its broad mutation/plugin surface is unnecessary for this tool.
- [sqlite-vec](https://github.com/asg017/sqlite-vec) and
  [LightRAG](https://github.com/HKUDS/LightRAG): reviewed as alternatives, not
  installed/reused. Vector/graph infrastructure is deferred until the baseline
  and independent evaluation justify it.
- [fxsjy/jieba](https://github.com/fxsjy/jieba) (MIT, verified via the
  [Arch package metadata](https://archlinux.org/packages/extra/any/python-jieba/):
  0.42.1, 17.7 MB package / **45.5 MB installed**): reviewed as the standard Chinese
  segmenter and **not reused**. Two reasons. One is size: a dictionary larger than
  everything this tool indexes. The other matters more — segmentation alone would
  not have fixed the Chinese queries. The corpus is English, so 退课 -> 退课
  segments perfectly and still matches nothing; what was missing was the mapping to
  English page vocabulary, which is what `glossary.json` supplies. jieba stays the
  obvious candidate if the corpus ever becomes Chinese, where segmentation would be
  the actual problem.
- [BYVoid/OpenCC](https://github.com/BYVoid/OpenCC) (Apache-2.0, C++, pushed 2026-09-25,
  10k stars): reviewed as the standard traditional/simplified converter and **not
  installed**. A wheel is ~2.0–2.9 MB of compiled code (sdist 11.6 MB) plus dictionaries
  (`STPhrases` ~1 MB); this tool requires zero new runtime dependencies and forbids an
  unreviewed bulk conversion. Its `STCharacters`/`TSCharacters` were used as a
  cross-check for the 27 hand-written variants, and every ambiguous character was
  decided by hand. No code or data is copied.
- [gumblex/zhconv](https://github.com/gumblex/zhconv) (pure Python): reviewed and
  **rejected for reuse**. Its repository LICENSE says MIT while its PyPI metadata says
  `GPLv2+`; an ambiguous license is not licensed for reuse, and it would be a new
  dependency anyway. Reference only.

## Embedding comparison: interface and acceptance contract (next stage; not deployed)

The measurement above says the lexical retriever is bounded by vocabulary. Comparing a
multilingual embedding arm is the obvious next experiment, but it is **not** part of this
stage and the small VPS must never host an embedder. This is the contract a later stage
would have to satisfy before any decision; nothing here is installed or wired.

* **Interface.** An offline `Embedder.embed(texts: list[str]) -> list[list[float]]` with a
  fixed dimension, L2 normalization, deterministic output for the same input, and no
  network access after model load. Retrieval becomes hybrid: the existing BM25
  `search_passages` plus an ANN/brute-force vector index keyed by `chunks.id`, fused by a
  stated rule (e.g. reciprocal-rank fusion) with the fusion weight fixed **before** looking
  at the held-out sets.
* **Data contract.** The same fixed snapshot (five pages, the `content_sha256` values
  above) and the four frozen case sets (`eval_cases`, `heldout_cases`,
  `heldout_independent_cases`, `heldout_next_cases`) plus `evidence_cases.json`, all pinned
  by hash. The vector store records model id, revision, dimension, normalization, pooling
  and the corpus `content_sha256`; a corpus rebuild re-embeds and bumps that record.
* **Acceptance plan.** Run all four sets and the evidence document twice (glossary off and
  on) on Mac **and** on the second machine, and report per-language page hit@1/@3,
  `evidence_all@3/@10`, and false candidates on the unanswerable classes. The embedding arm
  must beat the lexical baseline on at least one language **without** increasing false
  candidates on any negative class; a tie is a rejection, and abstention stays a separate,
  stated rule. No answer-level claims — the tool still has no generator. The acceptance
  numbers are read from the frozen sets, never from `eval_cases.json` alone.
* **Rollback.** Default off, exactly like `--glossary`: the lexical path remains the
  baseline and disabling the arm restores the current numbers byte for byte.

## Next gate (partly done, 2026-09-26)

Done: cross-language matching has a first, measured answer (`glossary.json`, opt-in),
`diagnose` separates a vocabulary gap from a ranking miss, abstention is a measured
opt-in gate with a calibration curve, one negative class is proven to be out of the
retriever's reach, the glossary is frozen by hash, the case sets now include one written
by a machine that never read the glossary plus a second-machine holdout, evidence-level
quotes and passage metrics exist, and the offline citation gate prototype is tested.

Not done, in the order they should be attacked:

1. **Traditional Chinese, measured and closed as a null result.** The 27 reviewed
   variants change rankings but no hit rate, and cost two false candidates on the
   second-machine holdout. The remaining traditional-positive misses are Cantonese
   domain words (`課堂`, `整點`, `宿舍`, `主修`), which is vocabulary review, not a
   conversion table. Do not grow the variant map expecting a recall win.
2. **Raise Chinese coverage past the glossary**, knowing it is not free: the independent
   set shows the expansion buying recall (zh 0.53 -> 0.74) while **costing precision**
   (negative false hits 4/5 -> 5/5); on the second-machine holdout it is 7/11 -> 9/11
   positives for 17/20 -> 18/20 false candidates. The fork the README has deferred since
   the start: grow the glossary by review, or evaluate multilingual embeddings (a new
   dependency — the second machine's owner plans a dedicated CPU embedder on their own
   box, not on the VPS) or LLM query rewriting (needs the model, so the eval can no
   longer run offline). Compare on the frozen sets, never on `eval_cases.json`.
3. **Citation validation at answer time** — the only place the `none03` class can be
   refused (see the abstention section), and the evidence-level metrics show why: at K=3
   the page hit rate halves when the passage has to be quoted. The prototype exists;
   wiring it to the answer step still needs prompt-injection isolation, which the
   prototype only flags, never solves.
4. **Corpus coverage and versions.** Five pages; key dates live in linked
   XLSX/JSON; regulations have multiple versions. More sources is a review and
   copyright decision, not a crawling decision.
5. **Only then**: default-off service integration, citation validation for both
   full/brief reports, prompt-injection isolation, privacy updates, and rollback.

Do not deploy this tool as a worker or put an embedding server on the small VPS.
