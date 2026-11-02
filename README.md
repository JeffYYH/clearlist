# ClearList

ClearList is a small Python/Flask utility for business administrators who need to prepare a customer CSV before importing it into another system. It shows the original file first, lets the administrator choose a small set of deterministic cleaning operations, and presents the exact saved result before download.

The product is deliberately local-first and reviewable:

- The original CSV stays intact in the session workspace.
- Cleaning is explicit: no inferred corrections, fuzzy matching, or hidden normalization.
- The preview is built from the saved output bytes that the download route serves.
- The browser workflow is Upload → Configure → Review & download.
- Bundled records are fictional. The running application does not call an LLM or send CSV content to an external service.

## Intended user and problem

A small-business administrator often receives a customer export with surrounding whitespace, blank rows, missing optional values, or exact duplicate records. They need a safe way to make bounded corrections without losing leading zeros, Unicode, embedded newlines, or an audit-friendly explanation of what changed.

ClearList addresses that narrow problem with a temporary browser session, a visible original table, deterministic operations, a change log, removed-row reasons, and downloads tied to the reviewed result ID.

## Run with Docker

The supported runtime target is Linux through Docker. The image uses the pinned Python 3.12 base image, installs exact runtime dependencies, runs as UID/GID \`10001:10001\`, and keeps temporary request files in memory.

\`\`\`sh
docker build -t csv-cleaner .
docker run --rm --read-only -p 127.0.0.1:8000:8000 \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m csv-cleaner
\`\`\`

Open [http://localhost:8000](http://localhost:8000). Stop with Ctrl+C.

The test image runs the same checks used by CI:

\`\`\`sh
docker build --target test -t csv-cleaner-test .
docker run --rm --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m csv-cleaner-test
\`\`\`

The current test stage runs Ruff lint, Ruff format verification, and pytest. It does not require a database, API key, volume, or external service.

For a local Python environment, the equivalent commands are:

\`\`\`sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/python -m pytest -q -p no:cacheprovider
\`\`\`

## Product workflow

### 1. Upload

Choose one UTF-8 comma-separated CSV with a header row. The fictional sample is available at [samples/fictional_customers.csv](samples/fictional_customers.csv).

### 2. Configure

ClearList reports records, columns, missing cells, fully empty rows, and exact duplicate extras. The original table remains visible while the administrator chooses:

1. Remove fully empty rows.
2. Trim surrounding whitespace in selected columns.
3. Fill missing cells in selected columns with administrator-provided text.
4. Remove exact duplicates after the selected transformations, retaining the first occurrence.

All options start off. The fixed order is empty-row removal → trim → fill → duplicate removal.

### 3. Review and download

The preview shows:

- retained records and operation counts;
- original versus proposed values with explicit Before/After labels;
- missing-value counts before and after;
- removed source rows and reasons;
- formula-like warnings for spreadsheet review;
- the saved result ID;
- primary \`cleaned.csv\` and secondary \`summary.json\` downloads.

A replaced upload, new preview, reset, expiry, or process restart invalidates older result links.

## Fictional sample walkthrough

Use [samples/fictional_customers.csv](samples/fictional_customers.csv).

1. Upload it. The original report is **10 records, 6 columns, 18 missing cells, 2 fully empty rows, and 1 duplicate extra**.
2. Enable **Remove fully empty rows** and **Remove exact duplicates**.
3. Enable trim for \`name\`, \`email\`, and \`city\`.
4. Enable fill for \`city\` and enter \`Unknown\`.
5. Preview. The expected result is **6 retained records**, **2 empty rows removed**, **2 duplicate rows removed**, **5 trims**, **1 fill**, and **4 missing cells remaining**.
6. Inspect the output table, change log, removed-row sources, and before/after missing counts.
7. Download both files. The CSV and summary carry the same result identity; the summary records the CSV SHA-256.

The saved screenshots in [docs/screenshots/](docs/screenshots/) are actual Flask/Jinja renders of this fictional sample, not design mockups:

- [Desktop preview](docs/screenshots/preview-desktop.jpg) — 1280×900.
- [Mobile preview](docs/screenshots/preview-mobile.jpg) — 500×900. The local headless Chrome used for capture clamps below-500px requests to a 500px CSS viewport; the narrow browser workflow was also inspected separately.

## Data contract and limits

- UTF-8 CSV, optionally with a UTF-8 BOM; LF and CRLF record separators are accepted.
- Unique, nonblank headers; comma delimiter; all values remain strings.
- Maximum **2 MiB input**, **10,000 data records**, **50 columns**, **100,000 characters per cell/header**, **200 characters per replacement**, and **10 MiB generated output**.
- Quoted commas, doubled quotes, embedded newlines, Unicode, header-only files, and blank physical records are supported.
- Invalid encoding, NUL characters, malformed or overly strict quotes, duplicate/blank headers, inconsistent row widths, and over-limit fields are rejected.
- Output is UTF-8 without an added BOM, comma-separated, standard quoted, and CRLF-terminated. It is parsed again before the result snapshot is stored.
- Missing means \`value.strip() == ""\`; \`NA\`, \`NULL\`, \`0\`, and other literal tokens are ordinary values.
- Duplicate matching is case-sensitive and uses every column after the selected operations.
- Data lives in a bounded process-local session store with four active sessions and a 30-minute idle expiry. Closing a tab does not immediately clear it.
- The intentional deployment limitation is one Gunicorn worker. Multi-worker hosting would require shared session storage and a different lifecycle design.

Potential spreadsheet formula prefixes are flagged but preserved, including \`+\` phone numbers. CSV quoting does not neutralize formulas; see [OWASP's CSV injection explanation](https://owasp.org/www-community/attacks/CSV_Injection).

## Architecture

\`\`\`mermaid
flowchart TD
    Browser[Browser] --> Web[Flask blueprint<br/>web.py]
    Web --> CSRF[Flask-WTF CSRF<br/>security headers]
    Web --> Engine[CSV engine<br/>engine.py]
    Web <--> Store[SessionStore<br/>state.py]
    Engine --> Snapshot[PreviewResult<br/>saved output snapshot]
    Store --> Snapshot
    Snapshot --> Templates[Jinja templates<br/>HTML preview]
    Snapshot --> Downloads[send_file<br/>cleaned.csv / summary.json]
\`\`\`

- \`engine.py\` parses CSV data, reports quality issues, applies the four operations, serializes output, and re-parses generated bytes.
- \`web.py\` owns the upload/configure/preview/download workflow, form validation, pagination, CSRF, response headers, and result ownership checks.
- \`state.py\` stores one immutable dataset and latest result per session key in a bounded process-local \`SessionStore\`.
- Templates are server-rendered HTML; CSS is served locally. There is no JavaScript framework, client-side CSV parser, database, or runtime AI service.

## Verification

The current local verification baseline is:

- **83 pytest tests passed**.
- Ruff lint passed.
- Ruff format check passed.
- \`git diff --check\` passed.
- Browser workflow checked with the fictional sample, including desktop and narrow preview renders, changed cells, removed rows, warnings, downloads, keyboard focus progression, and table-region overflow behavior.
- The GitHub Actions workflow at [.github/workflows/check.yml](.github/workflows/check.yml) builds the test image, runs the test stage, builds the runtime image, and smoke-checks \`/healthz\`.

The documented Docker commands are the CI commands. A Docker daemon was unavailable during the latest local documentation pass, so no new local Docker run is claimed here.

See [docs/ai-workflow.md](docs/ai-workflow.md) for the preserved development record, AI decisions, concurrency fix, test expansion, UI redesign, time accounting, and submission gaps.
