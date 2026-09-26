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
# The set that is allowed to disagree with the one above (see "The held-out set"):
.venv-pilot/bin/python tools/rag_corpus.py evaluate \
  --corpus /tmp/cityu-corpus-new.sqlite --cases tools/rag_data/heldout_cases.json --glossary
.venv-pilot/bin/python -m unittest pilot_app.tests.test_rag_offline -v
```

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
inline hidden styles; comments are not indexed. It is **not a browser/CSS engine**:
class-based hidden sections and repeated in-page enquiries may remain. Extracted
text is untrusted evidence, never instructions. Integration will need prompt
injection tests before any model receives these passages.

## Storage and retrieval contract

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

## The held-out set, and what it says (2026-09-26)

`heldout_cases.json` — 32 different questions (14 Chinese, 13 English, 2 mixed, 4
negatives), written **from the page text** after the glossary was committed, and
deliberately using Chinese vocabulary the glossary was not built around
(时间票, 候补名单, 上课时间, 星期六, 学院, 学费, 双主修, 通识课程 …):

| set | glossary | hit@1 | hit@3 | Chinese hit@3 | English | mixed | negative false hits |
|---|---|---|---|---|---|---|---|
| dev (30) | off | 0.654 | 0.692 | 0.0 | 1.0 | 1.0 | 2/4 |
| dev (30) | on | 0.846 | 1.000 | 1.00 | 1.0 | 1.0 | 2/4 |
| **held-out (32)** | off | 0.414 | 0.517 | 0.07 | 0.92 | 1.0 | 2/4 |
| **held-out (32)** | **on** | **0.552** | **0.759** | **0.57** | 0.92 | 1.0 | 2/4 |

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
itself. A genuinely independent author (the second machine, which has not read the
glossary) is still the next step; it was unreachable when this was measured.

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
The report includes corpus SHA-256 for reproducibility; no artificial pass gate
turns a failed retrieval benchmark into a production approval.

## Open-source review

- [SQLite FTS5](https://www.sqlite.org/fts5.html): actual reused capability (Python's
  existing SQLite runtime); BM25, FTS and query syntax reference.
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

## Next gate (partly done, 2026-09-26)

Done: cross-language matching has a first, measured answer (`glossary.json`, opt-in,
Chinese 0/8 -> 8/8 on the development set and 0.07 -> 0.57 on the held-out set),
`diagnose` separates a vocabulary gap from a ranking miss, abstention is a measured
opt-in gate with a calibration curve, one negative class is proven to be out of the
retriever's reach, and the glossary is frozen by hash against a second case set.

Not done, in the order they should be attacked:

1. **Raise Chinese coverage past the glossary.** 0.57 is the honest ceiling of a
   hand-curated term list, and the six remaining failures are all empty results from
   vocabulary it never covered. This is the fork the README has deferred since the
   start: grow the glossary by review, or evaluate multilingual embeddings (a new
   dependency, and not on the small VPS) or LLM query rewriting (needs the model, so
   the eval can no longer run offline). Compare on `heldout_cases.json`, which is
   already frozen.
2. **An independent author for the case set.** Both current sets were written by the
   same person who wrote the glossary. Only the second machine can fix that, and it
   must not read `glossary.json` while writing.
3. **Citation validation at answer time** — the only place the `none03` class can be
   refused (see the abstention section). That work belongs to the answer step this tool
   deliberately does not have, and it needs prompt-injection isolation first.
4. **Corpus coverage and versions.** Five pages; key dates live in linked
   XLSX/JSON; regulations have multiple versions. More sources is a review and
   copyright decision, not a crawling decision.
5. **Only then**: default-off service integration, citation validation for both
   full/brief reports, prompt-injection isolation, privacy updates, and rollback.

Do not deploy this tool as a worker or put an embedding server on the small VPS.
