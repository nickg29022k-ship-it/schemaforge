"""
Web app: upload a MySQL dump, set rows per table, download generated dummy dump.
Run: uvicorn app:app --reload
"""
import base64
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

from dotenv import load_dotenv

load_dotenv()  # load .env before reading env vars

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from generate_dummy_dump_llm import generate_dump

app = FastAPI(title="Database Dump Generator")
logger = logging.getLogger(__name__)
executor = ThreadPoolExecutor(max_workers=1)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")


@app.get("/", response_class=HTMLResponse)
def index():
    return get_index_html()


def get_index_html():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Dummy Dump Generator</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; max-width: 520px; margin: 2rem auto; padding: 0 1rem; }
    h1 { font-size: 1.5rem; margin-bottom: 0.5rem; }
    p { color: #555; font-size: 0.95rem; margin-bottom: 1.5rem; }
    label { display: block; font-weight: 600; margin-bottom: 0.35rem; }
    input[type="file"], input[type="number"] { width: 100%; padding: 0.5rem; margin-bottom: 1rem; border: 1px solid #ccc; border-radius: 6px; }
    input[type="number"] { max-width: 120px; }
    button { background: #2563eb; color: white; border: none; padding: 0.6rem 1.2rem; border-radius: 6px; font-weight: 600; cursor: pointer; }
    button:hover { background: #1d4ed8; }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    #message { margin-top: 1rem; padding: 0.75rem; border-radius: 6px; display: none; }
    #message.error { background: #fef2f2; color: #b91c1c; display: block; }
    #message.success { background: #f0fdf4; color: #166534; display: block; }
    .hint { font-size: 0.85rem; color: #666; margin-top: 0.25rem; }
  </style>
</head>
<body>
  <h1>Dummy Dump Generator</h1>
  <p>Upload a MySQL dump, choose how many rows per table, and download a new dump with LLM-generated dummy data. Uses up to 5 random rows per table from your dump as examples.</p>
  <form id="form">
    <label for="dump">SQL dump file</label>
    <input type="file" id="dump" name="dump" accept=".sql" required>
    <label for="rows">Rows per table</label>
    <input type="number" id="rows" name="rows" value="20" min="1" max="500">
    <span class="hint">Same count for every table (e.g. 20)</span>
    <div style="margin-top: 1rem;">
      <button type="submit" id="btn">Generate dummy dump</button>
    </div>
  </form>
  <div id="message"></div>
  <div id="progress" style="margin-top:0.75rem; font-size:0.9rem; color:#666; display:none;"></div>
  <script>
    const form = document.getElementById('form');
    const btn = document.getElementById('btn');
    const msg = document.getElementById('message');
    const progressEl = document.getElementById('progress');
    function show(m, isError) {
      msg.textContent = m;
      msg.className = isError ? 'error' : 'success';
      msg.style.display = 'block';
    }
    function showProgress(text) {
      progressEl.textContent = text;
      progressEl.style.display = text ? 'block' : 'none';
    }
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const file = document.getElementById('dump').files[0];
      const rows = document.getElementById('rows').value || 20;
      if (!file) { show('Please select a dump file.', true); return; }
      btn.disabled = true;
      show('Generating... This may take a few minutes.');
      showProgress('Starting...');
      const fd = new FormData();
      fd.append('dump', file);
      fd.append('rows_per_table', String(rows));
      try {
        const r = await fetch('/api/generate-stream', { method: 'POST', body: fd });
        if (!r.ok) {
          const t = await r.text();
          show(t || 'Generation failed.', true);
          showProgress('');
          btn.disabled = false;
          return;
        }
        const reader = r.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        let sqlB64 = null;
        let errors = null;
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const lines = buf.split('\\n');
          buf = lines.pop() || '';
          for (const line of lines) {
            if (line.startsWith('data: ')) {
              try {
                const data = JSON.parse(line.slice(6));
                if (data.current != null) {
                  showProgress('Generating table ' + (data.current) + '/' + data.total + ': ' + (data.table || '') + ' ...');
                } else if (data.sql_b64 != null) {
                  sqlB64 = data.sql_b64;
                  errors = data.errors || [];
                } else if (data.error) {
                  show(data.error, true);
                  showProgress('');
                  btn.disabled = false;
                  return;
                }
              } catch (_) {}
            }
          }
        }
        if (buf.startsWith('data: ')) {
          try {
            const data = JSON.parse(buf.slice(6));
            if (data.sql_b64 != null) { sqlB64 = data.sql_b64; errors = data.errors || []; }
            else if (data.error) { show(data.error, true); showProgress(''); btn.disabled = false; return; }
          } catch (_) {}
        }
        showProgress('');
        if (sqlB64) {
          const bin = Uint8Array.from(atob(sqlB64), c => c.charCodeAt(0));
          const a = document.createElement('a');
          a.href = URL.createObjectURL(new Blob([bin], { type: 'application/sql' }));
          a.download = (file.name || 'dump').replace(/\\.sql$/i, '') + '-dummy-llm.sql';
          a.click();
          URL.revokeObjectURL(a.href);
          show('Download started. Check your downloads.' + (errors && errors.length ? ' (' + errors.length + ' table(s) had errors.)' : ''));
        } else {
          show('No data received.', true);
        }
      } catch (err) {
        show('Request failed: ' + err.message, true);
        showProgress('');
      }
      btn.disabled = false;
    });
  </script>
</body>
</html>
"""


@app.post("/api/generate")
async def api_generate(
    dump: UploadFile = File(...),
    rows_per_table: int = Form(20),
):
    """Generate dummy dump and return it as a file download."""
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not set on the server.")
    if not dump.filename or not dump.filename.lower().endswith(".sql"):
        raise HTTPException(status_code=400, detail="Please upload a .sql file.")
    content = await dump.read()
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read file as text: {e}")
    try:
        sql_output, errors = generate_dump(
            text,
            rows_per_table=rows_per_table,
            api_key=OPENAI_API_KEY,
            model="gpt-4o-mini",
            max_sample_rows=5,
            progress_callback=lambda i, n, t: logger.info("Generating table %s (%s/%s)", t, i, n),
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Generation failed: {e}")
    filename = (dump.filename or "dump").replace(".sql", "").replace(".SQL", "") + "-dummy-llm.sql"
    return Response(
        content=sql_output.encode("utf-8"),
        media_type="application/sql",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/generate-stream")
async def api_generate_stream(
    dump: UploadFile = File(...),
    rows_per_table: int = Form(20),
):
    """Stream progress events (SSE) then final SQL as base64. Frontend shows table-by-table progress."""
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not set on the server.")
    if not dump.filename or not dump.filename.lower().endswith(".sql"):
        raise HTTPException(status_code=400, detail="Please upload a .sql file.")
    content = await dump.read()
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read file as text: {e}")

    queue = Queue()

    def worker():
        try:
            sql_output, errors = generate_dump(
                text,
                rows_per_table=rows_per_table,
                api_key=OPENAI_API_KEY,
                model="gpt-4o-mini",
                max_sample_rows=5,
                progress_callback=lambda i, n, t: queue.put(("progress", i, n, t)),
            )
            queue.put(("done", (sql_output, errors)))
        except Exception as e:
            queue.put(("error", str(e)))

    async def event_stream():
        import asyncio
        loop = asyncio.get_event_loop()
        loop.run_in_executor(executor, worker)
        while True:
            item = await loop.run_in_executor(None, queue.get)
            if item[0] == "progress":
                _, i, n, t = item
                yield f"data: {json.dumps({'current': i, 'total': n, 'table': t})}\n\n"
            elif item[0] == "error":
                yield f"data: {json.dumps({'error': item[1]})}\n\n"
                break
            else:
                _, (sql_output, errors) = item
                yield f"data: {json.dumps({'sql_b64': base64.b64encode(sql_output.encode()).decode(), 'errors': [[t, e] for t, e in errors]})}\n\n"
                break

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/info")
def api_info():
    """Return whether API key is configured and table count from last parse (not used by frontend)."""
    return {
        "openai_configured": bool(OPENAI_API_KEY),
        "max_sample_rows": 5,
    }
