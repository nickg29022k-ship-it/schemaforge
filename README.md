# Database Generator

Turn a real MySQL dump into a **realistic, schema-safe dummy dump** — same tables, same columns, same foreign-key relationships, but LLM-generated data instead of real (possibly sensitive) rows. Useful for sharing a workable dataset without shipping production data.

Available as a **web app** (drag-and-drop, progress bar, download) or a **CLI**.

## Why not just `mysqldump` + find/replace?

Real dumps carry PII and business-sensitive data you don't want in a demo environment, a test fixture, or a teammate's laptop. Naive anonymization (masking/replacing values in place) breaks foreign keys and referential integrity the moment two tables need to agree on an ID. This tool instead:

- **Parses the schema** — table structure, column types, and every `FOREIGN KEY ... REFERENCES` relationship from the dump itself (no separate schema file needed).
- **Orders generation by dependency** — parent tables (e.g. `restaurants`, `users`) are generated before the child tables that reference them (e.g. `bookings`, `auctions`), so FK values always exist when needed.
- **Constrains the LLM's output** — for every FK column, the prompt lists only the *actual* generated IDs from the parent table; a post-processing step (`merge_pk_fk`) forcibly corrects any value the model still gets wrong, so the result is guaranteed referentially consistent, not just usually-correct.
- **Samples real rows for style** — up to 5 existing rows per table are shown to the LLM as formatting/style examples (names, date formats, enum values), without ever including all your real data in the prompt.

## Web app

```bash
pip install -r requirements.txt
export OPENAI_API_KEY="sk-..."
uvicorn app:app --reload --host 0.0.0.0
```

Open **http://localhost:8000** → upload a `.sql` dump → set rows per table (e.g. 20) → **Generate dummy dump**. Progress streams table-by-table (Server-Sent Events) while the LLM works through each table in dependency order; the finished dump downloads automatically.

## CLI

```bash
export OPENAI_API_KEY="sk-..."
python generate_dummy_dump_llm.py path/to/dump.sql --rows 20 -o output-dummy.sql
```

Omit `--rows` for an interactive prompt (single count for all tables, or `custom` to set per-table counts).

## How it works

```
.sql dump
   │
   ▼
Parse CREATE TABLE + FOREIGN KEY blocks, sample up to 5 rows/table
   │
   ▼
Topologically sort tables (parents before children, by FK deps)
   │
   ▼
For each table:
  - generate deterministic new PK values
  - build a prompt: columns, types, FK-allowed-values, sample rows
  - ask the LLM (default gpt-4o-mini) for N rows as JSON
  - merge_pk_fk(): force PK/FK columns to the correct generated values
   │
   ▼
Reassemble into a valid mysqldump-format .sql file
```

If a table's LLM call fails, that table is left empty (schema preserved, marked with an error comment) and generation continues — one bad table won't abort the whole dump.

## Requirements

- Python 3.8+
- OpenAI API key ([platform.openai.com/api-keys](https://platform.openai.com/api-keys))
- `pip install -r requirements.txt` (`requirements-llm.txt` adds the same `openai` dep if you only want the CLI script)

## Input format

Expects a standard `mysqldump` output: `-- Table structure for table` markers, `CREATE TABLE ... ENGINE=` blocks, and `INSERT INTO ... VALUES (...)` blocks. Foreign keys must be declared inline as `CONSTRAINT ... FOREIGN KEY (`col`) REFERENCES `table` (`col`)` for the dependency graph to pick them up.

## Notes / limitations

- Primary keys are generated deterministically (not requested from the LLM) to guarantee uniqueness and FK-target availability — only non-key columns are LLM-generated.
- `rows_per_table` applies uniformly per request; there's no per-table override in the web app UI (the CLI's interactive `custom` mode does support it).
- Designed for MySQL dump syntax specifically; other dialects would need the regexes in `extract_schema_and_blocks` / `parse_create_table` adjusted.
