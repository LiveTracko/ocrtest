let selected = null;
let pollTimer = null;
let failCount = 0;

// All API calls go through here. Render's free tier returns its own HTML
// "waking/deploying" pages during restarts — never let that surface as a
// raw "Unexpected token '<'" SyntaxError. Show a human message + retry.
async function apiJson(url, opts) {
  let res;
  try {
    res = await fetch(url, opts);
  } catch (e) {
    throw new Error('cannot reach server (it may be waking up — free plan sleeps). Retrying…');
  }
  const ctype = res.headers.get('content-type') || '';
  if (!ctype.includes('application/json')) {
    throw new Error('server is restarting/waking (got a loading page, not data). Retrying…');
  }
  let j;
  try {
    j = await res.json();
  } catch (e) {
    throw new Error('server returned an empty reply (restarting?). Retrying…');
  }
  if (!res.ok) {
    throw new Error(j.detail || ('request failed (' + res.status + ')'));
  }
  return j;
}

function noteFail(box, e) {
  failCount++;
  box.innerHTML = '<p class="hint">⏳ ' + escapeHtml(e.message) +
    ' Auto-retries every 10s — or press Refresh. ' +
    '(Free plan sleeps after 15 min idle; first load can take ~1 min.)</p>';
}

async function health() {
  try {
    const j = await apiJson('/api/health');
    document.getElementById('health').textContent =
      `Server OK • model=${j.model} • key=${j.key_set ? 'set' : 'MISSING — set GEMINI_API_KEY on Render'} • DPI=${j.dpi}`;
  } catch (e) {
    document.getElementById('health').textContent = 'Server waking… refresh in a minute.';
  }
}

async function upload() {
  const f = document.getElementById('file').files[0];
  const msg = document.getElementById('uploadMsg');
  if (!f) { msg.textContent = 'Pick a PDF first.'; return; }
  msg.textContent = 'Uploading… (big PDFs take a while — do not close the tab)';
  const fd = new FormData();
  fd.append('file', f);
  fd.append('from_page', document.getElementById('fromPage').value || '');
  fd.append('to_page', document.getElementById('toPage').value || '');
  fd.append('force', document.getElementById('force').checked ? 'true' : 'false');
  fd.append('mode', document.getElementById('mode').value);
  try {
    const j = await apiJson('/api/jobs', { method: 'POST', body: fd });
    msg.textContent = 'Job started: ' + j.job_id + ' — watch progress below.';
    selected = j.job_id;
    loadJobs();
    watchJob(j.job_id);
  } catch (e) {
    msg.textContent = 'Upload issue: ' + e.message;
  }
}

async function loadJobs() {
  const box = document.getElementById('jobs');
  try {
    const j = await apiJson('/api/jobs');
    failCount = 0;
    if (!j.jobs.length) { box.innerHTML = '<p class="hint">No jobs yet — upload a PDF.</p>'; return; }
    let h = '<table><tr><th></th><th>Job</th><th>File</th><th>Status</th><th>Progress</th><th>Records</th><th>Download</th></tr>';
    for (const job of j.jobs) {
      const done = (job.done_pages || []).length;
      const total = (job.pages && job.pages.length) || job.total_pages || 0;
      h += `<tr>
        <td><input type="checkbox" class="mergeChk" value="${job.job_id}"></td>
        <td><a href="#" onclick="watchJob('${job.job_id}');return false;">${job.job_id.slice(0, 8)}</a></td>
        <td>${escapeHtml(job.filename || '')}</td>
        <td><span class="badge ${job.status}">${job.status}</span>${job.error ? ' ⚠' : ''}</td>
        <td>${done}/${total}</td>
        <td>${job.total_records || 0} (${job.review_records || 0} review)</td>
        <td>
          <a class="dl" href="/api/jobs/${job.job_id}/download?kind=voters">Excel</a>
          <a class="dl" href="/api/jobs/${job.job_id}/download?kind=review">Review</a>
          <a class="dl" href="/api/jobs/${job.job_id}/download?kind=report">Report</a>
        </td></tr>`;
    }
    box.innerHTML = h + '</table>';
  } catch (e) {
    noteFail(box, e);
  }
}

async function watchJob(id) {
  selected = id;
  failCount = 0;
  document.getElementById('preview').innerHTML = '';
  document.getElementById('dlRow').style.display = 'none';
  if (pollTimer) clearInterval(pollTimer);
  await refreshDetail();
  pollTimer = setInterval(refreshDetail, 4000);
}

async function refreshDetail() {
  if (!selected) return;
  try {
    const j = await apiJson('/api/jobs/' + selected);
    const done = (j.done_pages || []).length;
    const total = (j.pages && j.pages.length) || j.total_pages || 0;
    document.getElementById('detail').innerHTML =
      `<b>${selected.slice(0, 8)}</b> ${escapeHtml(j.filename || '')} — ` +
      `<span class="badge ${j.status}">${j.status}</span> ${done}/${total} pages, ` +
      `${j.total_records || 0} records ` +
      `<a class="dl" href="/api/jobs/${selected}/download?kind=voters">Excel</a>` +
      `<a class="dl" href="/api/jobs/${selected}/download?kind=review">Review</a>` +
      (j.error ? `<br>Error: ${escapeHtml(j.error)}` : '');
    const l = await apiJson(`/api/jobs/${selected}/logs?tail=120`);
    document.getElementById('logs').textContent = (l.logs || []).join('\n');
    // Wire the big Preview / Download buttons for this job.
    const dlRow = document.getElementById('dlRow');
    if (j.has_voters || j.has_review) {
      dlRow.style.display = 'flex';
      document.getElementById('dlExcel').href = `/api/jobs/${selected}/download?kind=voters`;
      document.getElementById('dlReview').href = `/api/jobs/${selected}/download?kind=review`;
    } else {
      dlRow.style.display = 'none';
    }
    if (j.status === 'done' || j.status === 'failed') { clearInterval(pollTimer); loadJobs(); }
  } catch (e) {
    // Job may vanish if the free server restarted (disk is temporary).
    // Keep polling a few times, then stop with a clear message.
    failCount++;
    if (failCount > 8) {
      clearInterval(pollTimer);
      document.getElementById('detail').innerHTML =
        '⚠ Lost contact with this job — the free server likely restarted (its disk is temporary, jobs vanish). ' +
        'Please re-upload the PDF. Tip: process small page ranges and download the Excel the same day.';
      document.getElementById('logs').textContent = '';
    } else {
      document.getElementById('detail').innerHTML = '⏳ ' + escapeHtml(e.message);
    }
  }
}

async function loadPreview() {
  if (!selected) return;
  const box = document.getElementById('preview');
  box.innerHTML = '<p class="hint">Loading preview…</p>';
  try {
    const p = await apiJson(`/api/jobs/${selected}/preview?limit=50`);
    if (!p.rows.length) { box.innerHTML = '<p class="hint">Excel is empty (no rows yet).</p>'; return; }
    let h = `<p class="hint">Showing ${p.shown_rows} of ${p.total_rows} rows from <b>${escapeHtml(p.file)}</b>. ` +
      `<a href="/api/jobs/${selected}/download?kind=voters">Download full Excel</a></p>` +
      '<div class="tblwrap"><table><tr>' +
      p.columns.map(c => `<th>${escapeHtml(c)}</th>`).join('') + '</tr>';
    for (const r of p.rows) {
      h += '<tr>' + p.columns.map(c => `<td>${escapeHtml(r[c] ?? '')}</td>`).join('') + '</tr>';
    }
    box.innerHTML = h + '</table></div>';
  } catch (e) {
    box.innerHTML = '<p class="hint">Preview not available yet: ' + escapeHtml(e.message) + '</p>';
  }
}

async function mergeJobs() {
  const ids = [...document.querySelectorAll('.mergeChk:checked')].map(c => c.value);
  const msg = document.getElementById('mergeMsg');
  if (ids.length < 2) { msg.textContent = 'Tick at least 2 jobs.'; return; }
  msg.textContent = 'Merging…';
  try {
    const j = await apiJson('/api/merge', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(ids)
    });
    msg.innerHTML = `Merged ${j.total_rows} rows → <a href="/api/jobs/${j.merge_job_id}/download?kind=voters">Download merged Excel</a>`;
    loadJobs();
  } catch (e) {
    msg.textContent = 'Merge issue: ' + e.message;
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

health();
loadJobs();
setInterval(loadJobs, 10000);
