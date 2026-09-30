/* Voter Roll Extractor — 100% browser version.
 * Flow per page: PDF.js render -> canvas JPEG -> Tesseract OCR (supporting) ->
 * Gemini Vision (image = truth) -> JS validation -> rows table -> SheetJS Excel.
 * Pages are sent ONE BY ONE, sequentially (kind to quota + memory).
 */
pdfjsLib.GlobalWorkerOptions.workerSrc =
  'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

const VOTER_COLUMNS = ["Page Number","Serial Number","EPIC Number","Name","Relation Type",
  "Relation Name","House Number","Age","Gender","Confidence","Validation Status","Review Reason"];
const REVIEW_COLUMNS = ["Page","Serial","EPIC","Name","Issue","Status"];
const EPIC_RE = /^[A-Z]{3}[0-9]{7}$/;
const VALID_GENDERS = ["Male","Female","Other"];
const VALID_RELATIONS = ["FATHER","MOTHER","HUSBAND","WIFE","OTHER"];

// Billed rates, USD per 1M tokens (Google AI Studio, 2026). Display only.
const RATES = {
  'gemini-3.8-flash': { in: 0.75, out: 3.75 },
  'gemini-2.5-flash': { in: 0.30, out: 2.50 },
  'gemini-3.5-flash-lite': { in: 0.30, out: 2.50 }
};
const USD_INR = 88; // approx display rate
let runCostUSD = 0, runPaidCalls = 0;

function renderCost() {
  document.getElementById('cost').textContent =
    `Cost this run: ₹${(runCostUSD * USD_INR).toFixed(2)} ($${runCostUSD.toFixed(4)}) • ${runPaidCalls} paid call(s)`;
}

function charge(model, usage, label) {
  const r = RATES[model] || RATES['gemini-3.8-flash'];
  const inp = usage.prompt || 0, out = usage.candidates || 0;
  const usd = inp / 1e6 * r.in + out / 1e6 * r.out;
  runCostUSD += usd; runPaidCalls++;
  renderCost();
  log(`${label}: tokens in=${inp} out=${out} • ₹${(usd * USD_INR).toFixed(2)}`);
}

function pagePrompt(pageNum, ocrText) {
  return `Extract voter records from this Indian electoral-roll page scan. IMAGE is truth; OCR below is supporting only (correct it from image). Never guess/invent/merge/split voters; unreadable -> null + needs_review.\nPage: ${pageNum}\nOCR (may err):\n---\n${ocrText}\n---\nReturn JSON ONLY, no fences:\n{"page_number": ${pageNum}, "voters": [{"serial_number": 1, "epic_number": "ABC1234567", "name": "RAMESH KUMAR", "relation_type": "FATHER", "relation_name": "SURESH KUMAR", "house_number": "125", "age": 45, "gender": "Male", "confidence": "HIGH", "needs_review": false, "review_reason": null}]}\nrelation_type: FATHER/MOTHER/HUSBAND/WIFE/OTHER/null. gender: Male/Female/Other/null. confidence HIGH only if all fields clear; else MEDIUM/LOW + needs_review + review_reason. Copy EPIC/house_number EXACTLY. age integer/null. Empty page -> {"page_number": N, "voters": []}.`;
}

let running = false, stopAsked = false;
let allVoters = [];   // {row..., _page, _source}
let pageIssues = [];  // {page, source, issue, status}
let doneCount = 0, totalCount = 0;

// ---------- helpers ----------
function log(m) {
  const el = document.getElementById('logs');
  el.textContent += new Date().toLocaleTimeString() + ' | ' + m + '\n';
  el.scrollTop = el.scrollHeight;
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function progress() {
  const pct = totalCount ? Math.round(doneCount / totalCount * 100) : 0;
  document.getElementById('barFill').style.width = pct + '%';
  document.getElementById('status').textContent = `Progress ${doneCount}/${totalCount} pages • ${allVoters.length} voter rows • ${pageIssues.length} review notes`;
  renderCost();
}

// ---------- page cache (same file+page+settings -> reuse, ₹0, identical data) ----------
function cacheKey(job, pageNum, model, scale, useOcr) {
  return [job.file.name, job.file.size, job.file.lastModified, pageNum, model, scale, useOcr ? 1 : 0].join('|');
}
function loadCache() { try { return JSON.parse(localStorage.getItem('vrCacheV1') || '{}'); } catch (e) { return {}; } }
function readCached(key) { const c = loadCache(); return c[key] || null; }
function writeCached(key, val) {
  try {
    const c = loadCache();
    c[key] = { ...val, savedAt: Date.now() };
    const keys = Object.keys(c);
    if (keys.length > 300) {
      keys.sort((a, b) => (c[a].savedAt || 0) - (c[b].savedAt || 0));
      for (const k of keys.slice(0, keys.length - 300)) delete c[k];
    }
    localStorage.setItem('vrCacheV1', JSON.stringify(c));
  } catch (e) { try { localStorage.removeItem('vrCacheV1'); } catch (e2) {} }
}

// ---------- blank-page check (white page + no OCR text -> skip paid call) ----------
function darkFraction(canvas) {
  const w = canvas.width, h = canvas.height;
  const stepX = Math.max(1, Math.floor(w / 200)), stepY = Math.max(1, Math.floor(h / 200));
  const tiny = document.createElement('canvas');
  tiny.width = Math.ceil(w / stepX); tiny.height = Math.ceil(h / stepY);
  tiny.getContext('2d').drawImage(canvas, 0, 0, tiny.width, tiny.height);
  const d = tiny.getContext('2d').getImageData(0, 0, tiny.width, tiny.height).data;
  let dark = 0, n = 0;
  for (let i = 0; i < d.length; i += 4) {
    n++;
    if ((d[i] + d[i + 1] + d[i + 2]) / 3 < 128) dark++;
  }
  return n ? dark / n : 0;
}

// ---------- API key ----------
function loadKey() {
  const k = localStorage.getItem('gemKey') || '';
  document.getElementById('apiKey').value = k;
  if (k) document.getElementById('keyMsg').textContent = 'Key saved in this browser ✓ (never sent anywhere except Google)';
}
function saveKey() {
  const k = document.getElementById('apiKey').value.trim();
  if (!k) { alert('Paste the key first.'); return; }
  localStorage.setItem('gemKey', k);
  document.getElementById('keyMsg').textContent = 'Key saved in this browser ✓ (never sent anywhere except Google)';
}
function toggleKey() {
  const i = document.getElementById('apiKey');
  i.type = i.type === 'password' ? 'text' : 'password';
}
loadKey();

// ---------- main ----------
async function start() {
  const key = (localStorage.getItem('gemKey') || document.getElementById('apiKey').value || '').trim();
  if (!key) { alert('Enter your Gemini API key first (section 1).'); return; }
  if (document.getElementById('apiKey').value.trim()) saveKey();
  const files = [...document.getElementById('files').files];
  if (!files.length) { alert('Choose at least one PDF.'); return; }
  const fromP = parseInt(document.getElementById('fromPage').value) || null;
  const toP = parseInt(document.getElementById('toPage').value) || null;
  if (fromP && toP && fromP > toP) { alert('From page cannot be greater than To page.'); return; }

  running = true; stopAsked = false;
  runCostUSD = 0; runPaidCalls = 0; renderCost();
  document.getElementById('startBtn').disabled = true;
  document.getElementById('stopBtn').disabled = false;
  document.getElementById('upMsg').textContent = 'Processing… do not close this tab.';
  log('Starting. Files: ' + files.map(f => f.name).join(', '));

  try {
    // count total first (fast, no AI cost)
    const jobs = [];
    for (const f of files) {
      const buf = await f.arrayBuffer();
      const pdf = await pdfjsLib.getDocument({ data: buf.slice(0) }).promise;
      const n = pdf.numPages;
      let lo = fromP || 1, hi = toP || n;
      if (lo < 1 || hi > n || lo > hi) throw new Error(`Page range ${lo}-${hi} invalid for ${f.name} (${n} pages).`);
      jobs.push({ file: f, pdf, lo, hi });
      totalCount += (hi - lo + 1);
    }
    progress();
    const model = document.getElementById('model').value;
    const scale = parseFloat(document.getElementById('quality').value);
    const useOcr = document.getElementById('useOcr').checked;

    for (const j of jobs) {
      for (let p = j.lo; p <= j.hi; p++) {
        if (stopAsked) { log('Stopped by user. Rows so far are kept — download anytime.'); break; }
        await processPage(j, p, key, model, scale, useOcr);
        doneCount++;
        progress();
        renderPreview();
      }
      if (stopAsked) break;
    }
    document.getElementById('upMsg').textContent =
      `Finished: ${allVoters.length} rows, ${pageIssues.length} review notes. Cost this run: ₹${(runCostUSD * USD_INR).toFixed(2)} (${runPaidCalls} paid calls). Download your Excel below.`;
    log(`Done. ${allVoters.length} rows. Download voters.xlsx now.`);
  } catch (e) {
    document.getElementById('upMsg').textContent = 'Error: ' + e.message;
    log('ERROR: ' + e.message);
  }
  running = false;
  document.getElementById('startBtn').disabled = false;
  document.getElementById('stopBtn').disabled = true;
}

function stop() { stopAsked = true; }

async function processPage(job, pageNum, key, model, scale, useOcr) {
  const label = `${job.file.name} p.${pageNum}`;
  const ck = cacheKey(job, pageNum, model, scale, useOcr);
  const hit = readCached(ck);
  if (hit) {
    log(`${label}: cache hit — reused stored rows, ₹0`);
    for (const r of hit.voters) allVoters.push({ ...r, _page: pageNum, _source: job.file.name });
    for (const i of hit.issues) pageIssues.push({ page: pageNum, source: job.file.name, issue: i, status: 'REVIEW' });
    if (hit.failed) pageIssues.push({ page: pageNum, source: job.file.name, issue: hit.failed, status: 'FAILED' });
    return;
  }
  log(`${label}: render…`);
  try {
    const page = await job.pdf.getPage(pageNum);
    const viewport = page.getViewport({ scale });
    const canvas = document.createElement('canvas');
    canvas.width = viewport.width; canvas.height = viewport.height;
    await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;

    const dark = darkFraction(canvas);
    let ocrText = '';
    if (useOcr) {
      log(`${label}: OCR…`);
      try {
        const r = await Tesseract.recognize(canvas, 'eng');
        ocrText = (r.data.text || '').slice(0, 12000);
      } catch (e) { log(`${label}: OCR skipped (${e.message})`); }
    }

    if (dark < 0.0003 && ocrText.trim().length < 20) {
      const note = 'blank page (no text found) — paid call skipped, verify visually';
      pageIssues.push({ page: pageNum, source: job.file.name, issue: note, status: 'REVIEW' });
      writeCached(ck, { voters: [], issues: [note], failed: null });
      log(`${label}: blank — paid call skipped, ₹0 (flagged for review)`);
      canvas.width = canvas.height = 0; // free memory
      return;
    }

    const jpg = canvas.toDataURL('image/jpeg', 0.85).split(',')[1];
    canvas.width = canvas.height = 0; // free memory
    log(`${label}: Gemini…`);
    const { data, usage } = await callGemini(key, model, pageNum, ocrText, jpg);
    charge(model, usage, label);
    const rows = validateRows(data, pageNum);
    for (const r of rows.voters) allVoters.push({ ...r, _page: pageNum, _source: job.file.name });
    for (const i of rows.issues) pageIssues.push({ page: pageNum, source: job.file.name, issue: i, status: 'REVIEW' });
    if (rows.failed) pageIssues.push({ page: pageNum, source: job.file.name, issue: rows.failed, status: 'FAILED' });
    writeCached(ck, { voters: rows.voters, issues: rows.issues, failed: rows.failed });
    log(`${label}: ${rows.voters.length} rows (${rows.voters.filter(v => v.needs_review).length} review)`);
  } catch (e) {
    pageIssues.push({ page: pageNum, source: job.file.name, issue: 'page failed: ' + e.message, status: 'FAILED' });
    log(`${label}: FAILED — ${e.message}`);
  }
}

async function callGemini(key, model, pageNum, ocrText, b64) {
  const url = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${encodeURIComponent(key)}`;
  const body = {
    contents: [{ parts: [{ text: pagePrompt(pageNum, ocrText) },
      { inline_data: { mime_type: 'image/jpeg', data: b64 } }] }],
    generationConfig: { responseMimeType: 'application/json', temperature: 0 }
  };
  let lastErr = 'unknown';
  for (let a = 1; a <= 3; a++) {
    try {
      const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (res.status === 429 || res.status >= 500) {
        lastErr = `Google busy (${res.status})`;
        await sleep(2000 * Math.pow(2, a - 1));
        continue;
      }
      if (!res.ok) {
        const t = await res.text();
        throw new Error(`Google error ${res.status}: ${t.slice(0, 200)}`);
      }
      const j = await res.json();
      let raw = (((j.candidates || [])[0] || {}).content || {}).parts || [];
      raw = raw.map(p => p.text || '').join('');
      const u = j.usageMetadata || {};
      return { data: extractJson(raw),
               usage: { prompt: u.promptTokenCount || 0, candidates: u.candidatesTokenCount || 0 } };
    } catch (e) {
      lastErr = e.message;
      if (/Google error 4/.test(e.message)) throw e; // bad key / bad request — retrying won't help
      await sleep(2000 * Math.pow(2, a - 1));
    }
  }
  throw new Error('Gemini failed after 3 tries: ' + lastErr);
}

function extractJson(raw) {
  let t = (raw || '').trim();
  if (!t) throw new Error('empty response from Gemini');
  if (t.startsWith('```')) { t = t.replace(/`/g, ''); if (t.toLowerCase().startsWith('json')) t = t.slice(4).trim(); }
  const d = JSON.parse(t);
  if (!d || !Array.isArray(d.voters)) throw new Error("Gemini JSON missing 'voters' list");
  return d;
}

// ---------- validation (flags, never drops) ----------
function validateRows(data, pageNum) {
  const voters = [], issues = [];
  let failed = null;
  if (data.page_number !== undefined && data.page_number !== pageNum)
    issues.push(`page_number mismatch: got ${data.page_number}, expected ${pageNum}`);
  if (!data.voters.length) issues.push('no voter records on page (cover/map/summary?) — verify visually');
  const seenEpic = {}, seenSerial = {};
  for (const rv of data.voters) {
    const v = {
      serial_number: rv.serial_number ?? null,
      epic_number: (rv.epic_number != null && rv.epic_number !== '') ? String(rv.epic_number).trim().toUpperCase() : null,
      name: rv.name ?? null,
      relation_type: rv.relation_type ?? null,
      relation_name: rv.relation_name ?? null,
      house_number: (rv.house_number != null && rv.house_number !== '') ? String(rv.house_number).trim() : null,
      age: Number.isInteger(rv.age) ? rv.age : (typeof rv.age === 'string' && rv.age.trim() !== '' && !isNaN(+rv.age) ? +rv.age : null),
      gender: rv.gender ?? null,
      confidence: ['HIGH','MEDIUM','LOW'].includes(rv.confidence) ? rv.confidence : 'LOW',
      needs_review: !!rv.needs_review,
      review_reason: rv.review_reason || null
    };
    const flag = (why) => { v.needs_review = true; v.review_reason = ((v.review_reason ? v.review_reason + '; ' : '') + why).slice(0, 500); };
    if (v.serial_number == null) { issues.push(`missing serial_number for name=${v.name}`); flag('missing serial'); }
    if (!v.name) { issues.push(`serial=${v.serial_number}: missing name`); flag('missing name'); }
    if (v.age != null && (v.age < 1 || v.age > 120)) { issues.push(`serial=${v.serial_number}: age ${v.age} out of range`); flag(`age ${v.age} out of range`); }
    if (v.gender != null && !VALID_GENDERS.includes(v.gender)) { issues.push(`serial=${v.serial_number}: unexpected gender '${v.gender}'`); flag(`unexpected gender '${v.gender}'`); }
    if (v.relation_type != null && !VALID_RELATIONS.includes(v.relation_type)) { issues.push(`serial=${v.serial_number}: unexpected relation '${v.relation_type}'`); flag(`unexpected relation`); }
    if (v.epic_number == null) { issues.push(`serial=${v.serial_number}: missing EPIC -> review`); flag('missing EPIC'); }
    else if (!EPIC_RE.test(v.epic_number)) { issues.push(`serial=${v.serial_number}: EPIC '${v.epic_number}' non-standard -> review`); flag('EPIC non-standard'); }
    if (v.epic_number) seenEpic[v.epic_number] = (seenEpic[v.epic_number] || 0) + 1;
    if (v.serial_number != null) seenSerial[v.serial_number] = (seenSerial[v.serial_number] || 0) + 1;
    voters.push(v);
  }
  for (const [e, n] of Object.entries(seenEpic)) if (n > 1) issues.push(`duplicate EPIC within page: ${e} x${n}`);
  for (const [s, n] of Object.entries(seenSerial)) if (n > 1) issues.push(`duplicate serial within page: ${s} x${n}`);
  return { voters, issues, failed };
}

// ---------- preview + Excel ----------
function renderPreview() {
  const box = document.getElementById('preview');
  if (!allVoters.length) { box.innerHTML = '<span class="hint">No rows yet…</span>'; return; }
  const last = allVoters.slice(-50);
  let h = `<p>Showing last ${last.length} of ${allVoters.length} rows. <span class="badge ok">${allVoters.filter(v=>!v.needs_review).length} OK</span> <span class="badge rev">${allVoters.filter(v=>v.needs_review).length} review</span></p><div class="tblwrap"><table><tr><th>Page</th><th>Serial</th><th>EPIC</th><th>Name</th><th>Age</th><th>Status</th></tr>`;
  for (const v of last) {
    h += `<tr><td>${esc(v._page)}</td><td>${esc(v.serial_number)}</td><td>${esc(v.epic_number)}</td><td>${esc(v.name)}</td><td>${esc(v.age)}</td><td>${v.needs_review ? '<span class="badge rev">REVIEW</span>' : '<span class="badge ok">OK</span>'}</td></tr>`;
  }
  box.innerHTML = h + '</table></div>';
}

function buildSheets() {
  const voterRows = allVoters.map(v => ({
    'Page Number': v._page, 'Serial Number': v.serial_number, 'EPIC Number': v.epic_number,
    'Name': v.name, 'Relation Type': v.relation_type, 'Relation Name': v.relation_name,
    'House Number': v.house_number, 'Age': v.age, 'Gender': v.gender, 'Confidence': v.confidence,
    'Validation Status': v.needs_review ? 'REVIEW' : 'OK', 'Review Reason': v.review_reason
  }));
  const reviewRows = [];
  for (const v of allVoters) if (v.needs_review)
    reviewRows.push({ 'Page': v._page, 'Serial': v.serial_number, 'EPIC': v.epic_number, 'Name': v.name, 'Issue': v.review_reason || 'needs review', 'Status': 'PENDING' });
  for (const p of pageIssues)
    reviewRows.push({ 'Page': p.page, 'Serial': null, 'EPIC': null, 'Name': null, 'Issue': `[${p.source}] ${p.issue}`, 'Status': p.status });
  if (!reviewRows.length) reviewRows.push({ 'Page': null, 'Serial': null, 'EPIC': null, 'Name': null, 'Issue': 'No records need review', 'Status': 'OK' });
  return { voterRows, reviewRows };
}

function downloadExcel(kind) {
  if (!allVoters.length && !pageIssues.length) { alert('Nothing to download yet — process pages first.'); return; }
  const { voterRows, reviewRows } = buildSheets();
  const wb = XLSX.utils.book_new();
  const ws = XLSX.utils.json_to_sheet(kind === 'voters' ? voterRows : reviewRows,
    { header: kind === 'voters' ? VOTER_COLUMNS : REVIEW_COLUMNS });
  ws['!cols'] = (kind === 'voters' ? VOTER_COLUMNS : REVIEW_COLUMNS).map(() => ({ wch: 18 }));
  XLSX.utils.book_append_sheet(wb, ws, kind === 'voters' ? 'VOTERS' : 'REVIEW');
  XLSX.writeFile(wb, kind === 'voters' ? 'voters.xlsx' : 'review.xlsx');
  document.getElementById('dlMsg').textContent = 'Downloaded ' + (kind === 'voters' ? 'voters.xlsx' : 'review.xlsx') + ' ✓';
}

function clearAll() {
  if (!confirm('Clear all results in this browser? (Downloaded Excels are safe.)')) return;
  allVoters = []; pageIssues = []; doneCount = 0; totalCount = 0;
  runCostUSD = 0; runPaidCalls = 0; renderCost();
  document.getElementById('preview').innerHTML = '';
  document.getElementById('logs').textContent = '';
  document.getElementById('barFill').style.width = '0%';
  document.getElementById('status').textContent = 'Cleared.';
}
