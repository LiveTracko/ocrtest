let selected = null;
let pollTimer = null;

async function health() {
  try {
    const r = await fetch('/api/health');
    const j = await r.json();
    document.getElementById('health').textContent =
      `Server OK • model=${j.model} • key=${j.key_set ? 'set' : 'MISSING — set GEMINI_API_KEY on Render'} • DPI=${j.dpi}`;
  } catch (e) {
    document.getElementById('health').textContent = 'Server unreachable.';
  }
}

async function upload() {
  const f = document.getElementById('file').files[0];
  const msg = document.getElementById('uploadMsg');
  if (!f) { msg.textContent = 'Pick a PDF first.'; return; }
  msg.textContent = 'Uploading…';
  const fd = new FormData();
  fd.append('file', f);
  fd.append('from_page', document.getElementById('fromPage').value || '');
  fd.append('to_page', document.getElementById('toPage').value || '');
  fd.append('force', document.getElementById('force').checked ? 'true' : 'false');
  fd.append('mode', document.getElementById('mode').value);
  try {
    const r = await fetch('/api/jobs', { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) { msg.textContent = 'Error: ' + (j.detail || r.status); return; }
    msg.textContent = 'Job started: ' + j.job_id;
    selected = j.job_id;
    loadJobs();
    watchJob(j.job_id);
  } catch (e) {
    msg.textContent = 'Upload failed: ' + e;
  }
}

async function loadJobs() {
  const box = document.getElementById('jobs');
  try {
    const r = await fetch('/api/jobs');
    const j = await r.json();
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
    box.textContent = 'Failed to load jobs: ' + e;
  }
}

async function watchJob(id) {
  selected = id;
  if (pollTimer) clearInterval(pollTimer);
  await refreshDetail();
  pollTimer = setInterval(refreshDetail, 2500);
}

async function refreshDetail() {
  if (!selected) return;
  try {
    const r = await fetch('/api/jobs/' + selected);
    const j = await r.json();
    const done = (j.done_pages || []).length;
    const total = (j.pages && j.pages.length) || j.total_pages || 0;
    document.getElementById('detail').innerHTML =
      `<b>${selected.slice(0, 8)}</b> ${escapeHtml(j.filename || '')} — ` +
      `<span class="badge ${j.status}">${j.status}</span> ${done}/${total} pages, ` +
      `${j.total_records || 0} records ` +
      `<a class="dl" href="/api/jobs/${selected}/download?kind=voters">Excel</a>` +
      `<a class="dl" href="/api/jobs/${selected}/download?kind=review">Review</a>` +
      (j.error ? `<br>Error: ${escapeHtml(j.error)}` : '');
    const l = await (await fetch(`/api/jobs/${selected}/logs?tail=120`)).json();
    document.getElementById('logs').textContent = (l.logs || []).join('\n');
    if (j.status === 'done' || j.status === 'failed') { clearInterval(pollTimer); loadJobs(); }
  } catch (e) { /* keep polling */ }
}

async function mergeJobs() {
  const ids = [...document.querySelectorAll('.mergeChk:checked')].map(c => c.value);
  const msg = document.getElementById('mergeMsg');
  if (ids.length < 2) { msg.textContent = 'Tick at least 2 jobs.'; return; }
  msg.textContent = 'Merging…';
  try {
    const r = await fetch('/api/merge', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(ids)
    });
    const j = await r.json();
    if (!r.ok) { msg.textContent = 'Merge error: ' + (j.detail || r.status); return; }
    msg.innerHTML = `Merged ${j.total_rows} rows → <a href="/api/jobs/${j.merge_job_id}/download?kind=voters">Download merged Excel</a>`;
    loadJobs();
  } catch (e) {
    msg.textContent = 'Merge failed: ' + e;
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

health();
loadJobs();
setInterval(loadJobs, 10000);
