#!/usr/bin/env python3
"""
Generic MySQL dump → dummy dump generator using an LLM.

Usage:
  export OPENAI_API_KEY="sk-..."   # or use --api-key
  python3 generate_dummy_dump_llm.py [path/to/dump.sql]

- Parses the dump for tables, columns, types, and FOREIGN KEYs (REFERENCES).
- Processes tables in dependency order (parents before children) so generated
  IDs for e.g. restaurants/users exist when generating auctions/bookings.
- Tells the LLM which columns are FKs and which IDs are allowed; then
  merge_pk_fk() forces invalid FK values to valid IDs from parent tables.
- Uses up to 5 random sample rows per table from the dump for style matching.

Requires: pip install openai
"""

import argparse
import json
import os
import re
import random
import sys

# OpenAI client imported in main() so --help works without pip install openai


# ---------------------------------------------------------------------------
# Dump parsing
# ---------------------------------------------------------------------------

def parse_create_table(body):
    """From CREATE TABLE body (between parens), return list of (col_name, type_str) and FK map."""
    lines = body.split("\n")
    columns = []
    fk_map = {}  # col_name -> (ref_table, ref_col)

    for line in lines:
        line = line.strip()
        if not line or line.upper().startswith("PRIMARY ") or line.upper().startswith("UNIQUE ") or line.upper().startswith("KEY "):
            continue
        # CONSTRAINT `name` FOREIGN KEY (`col`) REFERENCES `ref_table` (`ref_col`)
        fk_match = re.search(r"FOREIGN KEY\s*\(\s*`(\w+)`\s*\)\s*REFERENCES\s*`(\w+)`\s*\(\s*`(\w+)`\s*\)", line, re.I)
        if fk_match:
            col, ref_table, ref_col = fk_match.groups()
            fk_map[col] = (ref_table, ref_col)
            continue
        # `colname` type ...
        col_match = re.match(r"`(\w+)`\s+(\S+(?:\s*\([^)]+\))?(?:\s+\w+)*)", line)
        if col_match:
            col_name = col_match.group(1)
            type_part = col_match.group(2).strip()
            type_str = re.sub(r"\s+", " ", type_part)
            columns.append((col_name, type_str))

    return columns, fk_map


def extract_schema_and_blocks(content, max_sample_rows=5):
    """Return (header, footer, list of (table_name, structure, columns, fk_map, sample_rows)).
    sample_rows: up to max_sample_rows random rows from the table (or all if fewer).
    """
    table_pattern = re.compile(r"-- Table structure for table `(\w+)`", re.MULTILINE)
    matches = list(table_pattern.finditer(content))
    if not matches:
        return content, "", []

    header_end = matches[0].start()
    header = content[:header_end]

    create_re = re.compile(r"CREATE TABLE `(\w+)`\s*\(\s*\n(.*?)\n\s*\)\s+ENGINE=", re.DOTALL)
    insert_re = re.compile(r"INSERT INTO `(\w+)` VALUES\s*\((.*)\);", re.DOTALL)

    blocks = []
    for i, m in enumerate(matches):
        table_name = m.group(1)
        block_start = m.start()
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        block = content[block_start:block_end]
        idx = block.find("-- Dumping data for table")
        structure = block[:idx].rstrip() if idx != -1 else block
        while structure.endswith("--") or structure.endswith("\n--"):
            structure = structure.rsplit("\n", 1)[0].rstrip()

        create_m = create_re.search(block)
        columns, fk_map = [], {}
        if create_m:
            body = create_m.group(2)
            columns, fk_map = parse_create_table(body)

        sample_rows = []
        ins_m = insert_re.search(block)
        if ins_m and columns:
            values_str = ins_m.group(2).strip()
            if values_str:
                all_rows = split_all_row_values(values_str, len(columns))
                if all_rows:
                    n_sample = min(max_sample_rows, len(all_rows))
                    chosen = random.sample(all_rows, n_sample) if len(all_rows) >= n_sample else all_rows
                    col_names = [c[0] for c in columns]
                    sample_rows = [dict(zip(col_names, row[:len(columns)])) for row in chosen]

        blocks.append((table_name, structure, columns, fk_map, sample_rows))

    last_start = matches[-1].start()
    footer_start = content.find("/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */", last_start)
    if footer_start == -1:
        footer_start = content.rfind("UNLOCK TABLES;")
        if footer_start != -1:
            footer_start = content.index("\n", footer_start) + 1
        else:
            footer_start = len(content)
    footer = content[footer_start:] if footer_start < len(content) else ""

    return header, footer, blocks


def split_all_row_values(s, num_columns):
    """Extract all rows from VALUES (v1,v2),(v3,v4),... Return list of rows (each = list of values).
    s is the content between the first ( and last ); so it may be 'v1,v2),(v2,v3)' without leading (.
    """
    rows = []
    depth = 0
    in_quote = None
    cur = []
    row_vals = []
    i = 0
    # Values string can be "a,b),(c,d" so we pretend we start inside first row
    if s.strip().startswith("("):
        pass  # normal start
    else:
        depth = 1  # we're already inside the first row
    while i < len(s):
        c = s[i]
        if in_quote:
            if c == "\\" and i + 1 < len(s):
                cur.append(s[i + 1])
                i += 2
                continue
            if c == in_quote:
                in_quote = None
            cur.append(c)
            i += 1
            continue
        if c in ("'", '"'):
            in_quote = c
            cur.append(c)
            i += 1
            continue
        if c == "(":
            depth += 1
            if depth == 1:
                cur = []
                row_vals = []
            i += 1
            continue
        if c == ")":
            if depth == 1:
                val = "".join(cur).strip()
                if val:
                    row_vals.append(parse_sql_value(val))
                if len(row_vals) >= num_columns:
                    rows.append(row_vals[:num_columns])
                cur = []
                row_vals = []
            depth -= 1
            i += 1
            continue
        if c == "," and depth == 1:
            val = "".join(cur).strip()
            row_vals.append(parse_sql_value(val))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    # If we started with depth=1 we may have one row left in row_vals
    if depth == 1 and row_vals and len(row_vals) >= num_columns:
        rows.append(row_vals[:num_columns])
    return rows


def parse_row_values(row_str, num_columns):
    """Parse one row string 'v1,v2,v3' into list of Python values."""
    out = []
    cur = []
    depth = 0
    in_quote = None
    i = 0
    s = row_str.strip()
    while i < len(s):
        c = s[i]
        if in_quote:
            if c == "\\" and i + 1 < len(s):
                cur.append(s[i + 1])
                i += 2
                continue
            if c == in_quote:
                in_quote = None
            cur.append(c)
            i += 1
            continue
        if c in ("'", '"'):
            in_quote = c
            cur.append(c)
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == "," and depth == 0:
            out.append(parse_sql_value("".join(cur).strip()))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    if cur:
        out.append(parse_sql_value("".join(cur).strip()))
    return out if len(out) >= num_columns else None


def split_first_row_values(s):
    """Extract first row from VALUES (v1,v2),(v3,v4)... and return list of Python values."""
    depth = 0
    in_quote = None
    row = []
    cur = []
    i = 0
    while i < len(s):
        c = s[i]
        if in_quote:
            if c == "\\" and i + 1 < len(s):
                cur.append(s[i + 1])
                i += 2
                continue
            if c == in_quote:
                in_quote = None
            cur.append(c)
            i += 1
            continue
        if c in ("'", '"'):
            in_quote = c
            cur.append(c)
            i += 1
            continue
        if c == "(":
            depth += 1
            if depth == 1:
                cur = []
            i += 1
            continue
        if c == ")":
            if depth == 1:
                val = "".join(cur).strip()
                if val:
                    row.append(parse_sql_value(val))
                return row
            depth -= 1
            i += 1
            continue
        if c == "," and depth == 1:
            val = "".join(cur).strip()
            row.append(parse_sql_value(val))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    return row if row else None


def parse_sql_value(v):
    """Convert a single SQL value string to Python (for sample row)."""
    v = v.strip()
    if v.upper() == "NULL":
        return None
    if v.startswith("'") and v.endswith("'"):
        return v[1:-1].replace("\\'", "'").replace("\\\\", "\\")
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


# ---------------------------------------------------------------------------
# Row count prompt
# ---------------------------------------------------------------------------

def get_rows_per_table(table_names, default_n=10):
    """Ask user for row counts. Returns dict table_name -> int."""
    print(f"\nThere are {len(table_names)} tables. How many dummy rows per table?")
    print("Enter a single number (e.g. 20) for all tables, or 'custom' to set per table.")
    try:
        line = input("Rows per table [default=10]: ").strip() or str(default_n)
    except EOFError:
        line = str(default_n)
    if line.lower() == "custom":
        counts = {}
        for t in table_names:
            try:
                n = input(f"  {t}: ").strip() or str(default_n)
                counts[t] = int(n)
            except (ValueError, EOFError):
                counts[t] = default_n
        return counts
    try:
        n = int(line)
        return {t: n for t in table_names}
    except ValueError:
        return {t: default_n for t in table_names}


# ---------------------------------------------------------------------------
# ID generation and FK resolution
# ---------------------------------------------------------------------------

def generate_ids(n, prefix_hex=0x100):
    """Generate n UUID-like strings (same style as existing dumps)."""
    base = "11111111-2222-3333-4444-55556666"
    return [base + f"{(prefix_hex + i):03x}" for i in range(n)]


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def build_prompt(table_name, columns, fk_map, n_rows, pk_col, pk_values, fk_allowed, sample_rows):
    """Build the prompt for the LLM to generate n_rows of dummy data. sample_rows: list of dicts (up to 5)."""
    col_descriptions = []
    for col_name, type_str in columns:
        desc = f"- {col_name}: {type_str}"
        if col_name in fk_map:
            ref_t, _ = fk_map[col_name]
            allowed = fk_allowed.get(col_name, [])
            desc += f" (FK to {ref_t}; use ONLY these values: {allowed[:5]}{'...' if len(allowed) > 5 else ''})"
        elif pk_col and col_name == pk_col:
            desc += f" (PRIMARY KEY; use exactly these values in order: {pk_values[:3]}{'...' if len(pk_values) > 3 else ''})"
        col_descriptions.append(desc)

    prompt = f"""Generate {n_rows} rows of realistic dummy data for a MySQL table named `{table_name}`.

Columns (use these exact names and respect types and constraints):
{chr(10).join(col_descriptions)}

Rules:
- For PRIMARY KEY column: use the exact values provided, in order, one per row.
- For FK columns: use ONLY values from the allowed list (pick any valid one per row).
- For datetime: use format 'YYYY-MM-DD HH:MM:SS'.
- For enum: use one of the values from the type definition.
- For NULLable columns you may use null.
- Generate varied, realistic-looking data (names, emails, amounts, etc.).

Output a JSON array of {n_rows} objects. Each object must have every column name as key. No markdown, no explanation, only the JSON array.
Example format: [{{"id":"...", "name":"...", ...}}, ...]
"""
    if sample_rows:
        prompt += f"\nExample rows from the existing dump (match style and formats):\n{json.dumps(sample_rows[:5], default=str)[:1500]}\n"
    return prompt


def call_llm(client, model, prompt, table_name):
    """Call OpenAI and return parsed JSON array of rows."""
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You generate dummy SQL data. Reply only with a valid JSON array of objects; no markdown or explanation."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )
    text = response.choices[0].message.content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text).replace("```", "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned invalid JSON for {table_name}: {e}\nFirst 500 chars: {text[:500]}")
    if not isinstance(data, list):
        raise ValueError(f"LLM did not return a list for {table_name}")
    return data


def merge_pk_fk(rows, columns, pk_col, pk_values, fk_map, fk_allowed):
    """Ensure each row has correct PK and FK values; return list of tuples in column order."""
    col_names = [c[0] for c in columns]
    out = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        row = dict((k, v) for k, v in row.items())  # copy
        if i < len(pk_values) and pk_col:
            row[pk_col] = pk_values[i]
        for col, (ref_t, _) in (fk_map or {}).items():
            allowed = (fk_allowed or {}).get(col, [])
            if not allowed or col not in row:
                continue
            val = row.get(col)
            if val is None or val == "null" or str(val).strip() == "":
                continue
            if str(val) not in [str(a) for a in allowed]:
                row[col] = allowed[i % len(allowed)]
        out.append(tuple(row.get(c) for c in col_names))
    return out


def infer_pk_column(columns, fk_map):
    """Usually the first column is the PK."""
    for col, _ in columns:
        if col not in fk_map:
            return col
    return columns[0][0] if columns else None


def order_blocks_by_fk_deps(blocks):
    """
    Return blocks in topological order: parent tables before children.
    So when we generate a table, all tables it references (via FK) have already been generated.
    """
    name_to_block = {b[0]: b for b in blocks}
    table_names = list(name_to_block.keys())

    def deps(table_name):
        _, _, _, fk_map, _ = name_to_block[table_name]
        return {ref_t for ref_t, _ in fk_map.values() if ref_t in table_names and ref_t != table_name}

    out = []
    remaining = set(table_names)
    while remaining:
        chosen = None
        for t in remaining:
            if deps(t).issubset({b[0] for b in out}):
                chosen = t
                break
        if chosen is None:
            chosen = min(remaining, key=lambda t: len(deps(t) - {b[0] for b in out}))
        remaining.discard(chosen)
        out.append(name_to_block[chosen])
    return out


# ---------------------------------------------------------------------------
# SQL escaping and output
# ---------------------------------------------------------------------------

def esc(v):
    if v is None or (isinstance(v, str) and v.lower() == "null"):
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, bool):
        return "1" if v else "0"
    s = str(v).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{s}'"


# ---------------------------------------------------------------------------
# API-friendly generator (for web app)
# ---------------------------------------------------------------------------

def generate_dump(content, rows_per_table, api_key, model="gpt-4o-mini", max_sample_rows=5, progress_callback=None):
    """
    Generate dummy dump from dump content. Returns (sql_string, errors).
    rows_per_table: int (same for all) or dict table_name -> int.
    progress_callback: optional callable(current_index_1based, total_tables, table_name) called at start of each table.
    """
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("Install openai: pip install openai")
    client = OpenAI(api_key=api_key)
    header, footer, blocks = extract_schema_and_blocks(content, max_sample_rows=max_sample_rows)
    blocks = order_blocks_by_fk_deps(blocks)
    table_names = [b[0] for b in blocks]
    total = len(blocks)
    if isinstance(rows_per_table, int):
        rows_per_table = {t: rows_per_table for t in table_names}
    generated_pks = {}
    output_lines = [header.rstrip()]
    errors = []
    for idx, (table_name, structure, columns, fk_map, sample_rows) in enumerate(blocks, start=1):
        if progress_callback:
            progress_callback(idx, total, table_name)
        if not columns:
            output_lines.append("")
            output_lines.append(structure)
            output_lines.append("")
            output_lines.append("--")
            output_lines.append(f"-- Dumping data for table `{table_name}` (no columns parsed)")
            output_lines.append("--")
            output_lines.append("LOCK TABLES `" + table_name + "` WRITE;")
            output_lines.append("/*!40000 ALTER TABLE `" + table_name + "` DISABLE KEYS */;")
            output_lines.append("-- No data")
            output_lines.append("/*!40000 ALTER TABLE `" + table_name + "` ENABLE KEYS */;")
            output_lines.append("UNLOCK TABLES;")
            continue
        n = rows_per_table.get(table_name, 10)
        pk_col = infer_pk_column(columns, fk_map)
        pk_values = generate_ids(n, prefix_hex=hash(table_name) % 0x1000)
        generated_pks[table_name] = pk_values
        fk_allowed = {col: generated_pks.get(ref_t, []) for col, (ref_t, _) in fk_map.items()}
        prompt = build_prompt(table_name, columns, fk_map, n, pk_col, pk_values, fk_allowed, sample_rows)
        try:
            rows = call_llm(client, model, prompt, table_name)
        except Exception as e:
            errors.append((table_name, str(e)))
            output_lines.append("")
            output_lines.append(structure)
            output_lines.append("")
            output_lines.append("--")
            output_lines.append(f"-- Dumping data for table `{table_name}` (error)")
            output_lines.append("--")
            output_lines.append("LOCK TABLES `" + table_name + "` WRITE;")
            output_lines.append("/*!40000 ALTER TABLE `" + table_name + "` DISABLE KEYS */;")
            output_lines.append("-- Error: " + str(e)[:200])
            output_lines.append("/*!40000 ALTER TABLE `" + table_name + "` ENABLE KEYS */;")
            output_lines.append("UNLOCK TABLES;")
            continue
        merged = merge_pk_fk(rows, columns, pk_col, pk_values, fk_map, fk_allowed)
        if len(merged) > n:
            merged = merged[:n]
        while len(merged) < n and len(pk_values) > len(merged):
            extra = list(merged[-1]) if merged else [None] * len(columns)
            col_names = [c[0] for c in columns]
            idx = col_names.index(pk_col) if pk_col in col_names else 0
            extra[idx] = pk_values[len(merged)]
            merged.append(tuple(extra))
        output_lines.append("")
        output_lines.append(structure)
        output_lines.append("")
        output_lines.append("--")
        output_lines.append(f"-- Dumping data for table `{table_name}`")
        output_lines.append("--")
        output_lines.append("")
        output_lines.append("LOCK TABLES `" + table_name + "` WRITE;")
        output_lines.append("/*!40000 ALTER TABLE `" + table_name + "` DISABLE KEYS */;")
        values_str = ",".join("(" + ",".join(esc(v) for v in row) + ")" for row in merged)
        output_lines.append("INSERT INTO `" + table_name + "` VALUES " + values_str + ";")
        output_lines.append("/*!40000 ALTER TABLE `" + table_name + "` ENABLE KEYS */;")
        output_lines.append("UNLOCK TABLES;")
    output_lines.append("")
    output_lines.append(footer.lstrip())
    return "\n".join(output_lines), errors


# ---------------------------------------------------------------------------
# Main (CLI)
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate dummy MySQL dump using LLM")
    parser.add_argument("dump", nargs="?", default="hsv-develop.sql", help="Path to MySQL dump file")
    parser.add_argument("-o", "--output", default=None, help="Output dump path (default: <dump>-dummy-llm.sql)")
    parser.add_argument("--api-key", default=None, help="OpenAI API key (default: OPENAI_API_KEY env)")
    parser.add_argument("--model", default="gpt-4o-mini", help="OpenAI model (e.g. gpt-4o-mini, gpt-4o)")
    parser.add_argument("--default-rows", type=int, default=10, help="Default rows per table when not custom")
    parser.add_argument("--rows", type=int, default=None, help="Use this many rows for every table (non-interactive)")
    args = parser.parse_args()

    dump_path = args.dump
    if not os.path.isfile(dump_path):
        print(f"Error: dump file not found: {dump_path}", file=sys.stderr)
        sys.exit(1)

    api_key = args.api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Set OPENAI_API_KEY or pass --api-key", file=sys.stderr)
        sys.exit(1)

    try:
        from openai import OpenAI
    except ImportError:
        print("Install the OpenAI client: pip install openai", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(api_key=api_key)

    with open(dump_path, "r") as f:
        content = f.read()

    header, footer, blocks = extract_schema_and_blocks(content)
    blocks = order_blocks_by_fk_deps(blocks)
    table_names = [b[0] for b in blocks]
    print(f"Parsed {len(table_names)} tables from {dump_path}")

    if args.rows is not None:
        rows_per_table = {t: args.rows for t in table_names}
        print(f"Using {args.rows} rows per table (--rows)")
    else:
        rows_per_table = get_rows_per_table(table_names, default_n=args.default_rows)

    out_path = args.output or dump_path.replace(".sql", "-dummy-llm.sql")
    if out_path == dump_path:
        out_path = dump_path.replace(".sql", "-dummy-llm.sql")

    client = OpenAI(api_key=api_key)
    generated_pks = {}  # table_name -> list of PK values

    output_lines = [header.rstrip()]

    for table_name, structure, columns, fk_map, sample_rows in blocks:
        if not columns:
            output_lines.append("")
            output_lines.append(structure)
            output_lines.append("")
            output_lines.append("--")
            output_lines.append(f"-- Dumping data for table `{table_name}` (no columns parsed)")
            output_lines.append("--")
            output_lines.append("LOCK TABLES `" + table_name + "` WRITE;")
            output_lines.append("/*!40000 ALTER TABLE `" + table_name + "` DISABLE KEYS */;")
            output_lines.append("-- No data (schema only)")
            output_lines.append("/*!40000 ALTER TABLE `" + table_name + "` ENABLE KEYS */;")
            output_lines.append("UNLOCK TABLES;")
            continue

        n = rows_per_table.get(table_name, args.default_rows)
        pk_col = infer_pk_column(columns, fk_map)
        pk_values = generate_ids(n, prefix_hex=hash(table_name) % 0x1000)
        generated_pks[table_name] = pk_values

        fk_allowed = {}
        for col, (ref_table, _) in fk_map.items():
            fk_allowed[col] = generated_pks.get(ref_table, [])

        prompt = build_prompt(table_name, columns, fk_map, n, pk_col, pk_values, fk_allowed, sample_rows)

        print(f"  Generating {n} rows for `{table_name}`...", end=" ", flush=True)
        try:
            rows = call_llm(client, args.model, prompt, table_name)
        except Exception as e:
            print(f"Error: {e}")
            output_lines.append("")
            output_lines.append(structure)
            output_lines.append("")
            output_lines.append("--")
            output_lines.append(f"-- Dumping data for table `{table_name}` (LLM error)")
            output_lines.append("--")
            output_lines.append("LOCK TABLES `" + table_name + "` WRITE;")
            output_lines.append("/*!40000 ALTER TABLE `" + table_name + "` DISABLE KEYS */;")
            output_lines.append("-- Error: " + str(e)[:200])
            output_lines.append("/*!40000 ALTER TABLE `" + table_name + "` ENABLE KEYS */;")
            output_lines.append("UNLOCK TABLES;")
            continue

        merged = merge_pk_fk(rows, columns, pk_col, pk_values, fk_map, fk_allowed)
        if len(merged) > n:
            merged = merged[:n]
        while len(merged) < n and len(pk_values) > len(merged):
            extra = list(merged[-1]) if merged else [None] * len(columns)
            col_names = [c[0] for c in columns]
            idx = col_names.index(pk_col) if pk_col in col_names else 0
            extra[idx] = pk_values[len(merged)]
            merged.append(tuple(extra))

        output_lines.append("")
        output_lines.append(structure)
        output_lines.append("")
        output_lines.append("--")
        output_lines.append(f"-- Dumping data for table `{table_name}`")
        output_lines.append("--")
        output_lines.append("")
        output_lines.append("LOCK TABLES `" + table_name + "` WRITE;")
        output_lines.append("/*!40000 ALTER TABLE `" + table_name + "` DISABLE KEYS */;")
        values_str = ",".join("(" + ",".join(esc(v) for v in row) + ")" for row in merged)
        output_lines.append("INSERT INTO `" + table_name + "` VALUES " + values_str + ";")
        output_lines.append("/*!40000 ALTER TABLE `" + table_name + "` ENABLE KEYS */;")
        output_lines.append("UNLOCK TABLES;")
        print("ok")

    output_lines.append("")
    output_lines.append(footer.lstrip())

    with open(out_path, "w") as f:
        f.write("\n".join(output_lines))

    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
