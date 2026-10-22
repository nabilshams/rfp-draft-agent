'use strict';
/* RFP Draft Agent UI.
 *
 * Everything that comes from the server, an uploaded RFP or the model is untrusted. It is either put into the
 * page with textContent, or run through esc() / safeUrl() before it is placed in an HTML string. There are no
 * inline event handlers: the page is served with a Content-Security-Policy that only allows scripts from /static.
 */
const API = location.origin;
let currentRfpId = null, currentFilename = null;
let extractedRequirements = [], extractedQuestions = [], extractedEvaluationCriteria = [];
let generatedResponses = [];         // per-requirement responses, for document generation
let generatedSectionNarratives = []; // section narratives, for per_section document generation
let templateSectionsData = [];

const $ = id => document.getElementById(id);

// ── Safe output helpers (esc and safeUrl come from safe.js) ──

const MEETS_CLASSES = { Yes: 'meets-yes', Partial: 'meets-partial', No: 'meets-no' };
const meetsLabel = meets => (meets in MEETS_CLASSES ? meets : 'No');
const PRIORITY_CLASSES = { mandatory: 'badge-mandatory', preferred: 'badge-preferred' };
const priorityClass = priority => PRIORITY_CLASSES[priority] || 'badge-standard';
const num = value => (Number.isFinite(Number(value)) ? Number(value) : 0);

function normReqId(id) { return String(id ?? '').trim(); }
const respDomId = id => 'resp-' + encodeURIComponent(normReqId(id));
const blockDomId = id => 'req-block-' + encodeURIComponent(normReqId(id));
const outputUrl = (rfpId, file) => API + '/outputs/' + encodeURIComponent(rfpId) + '/' + file;

/** A readable message from a failed fetch (the API reports errors as {"detail": ...}). */
async function failureText(response) {
  const text = await response.text();
  try {
    const detail = JSON.parse(text).detail;
    if (typeof detail === 'string') return detail;
    if (detail) return JSON.stringify(detail);
  } catch { /* not JSON */ }
  return text;
}
async function fetchJson(path, options) {
  const response = await fetch(API + path, options);
  if (!response.ok) throw new Error(await failureText(response));
  return response.json();
}
const postJson = (path, body) =>
  fetchJson(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

function toast(message, type = 'success') {
  const el = $('toast');
  el.textContent = message;
  el.className = 'toast ' + type + ' visible';
  setTimeout(() => el.classList.remove('visible'), 3500);
}

const SPINNER_HTML = '<div style="display:flex;align-items:center;gap:6px"><div class="spinner"></div><em>Processing...</em></div>';
const setLoadingText = (loadingEl, text) => { loadingEl.querySelector('span').textContent = text; };

// ── Tabs, health, drag and drop ──────────────────────────

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    $('tab-' + tab.dataset.tab).classList.add('active');
    if (tab.dataset.tab === 'dashboard') loadDashboard();
    if (tab.dataset.tab === 'logs') connectLogWs();
    if (tab.dataset.tab === 'template') loadTemplateInfo();
    if (tab.dataset.tab === 'kb') loadKbList();
  });
});

async function checkHealth() {
  try {
    const health = await fetchJson('/health');
    for (const id of ['draftWebRow', 'askWebRow']) $(id).style.display = health.web_search_available ? 'block' : 'none';
    $('statusDot').classList.remove('offline');
    $('statusText').textContent = 'Connected';
  } catch {
    $('statusDot').classList.add('offline');
    $('statusText').textContent = 'Offline';
  }
}
checkHealth();
setInterval(checkHealth, 30000);

function setupDragDrop(zoneId, inputId, onFiles) {
  const zone = $(zoneId), input = $(inputId);
  zone.addEventListener('click', () => input.click());
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('dragover');
    if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; onFiles(input.files); }
  });
  input.addEventListener('change', () => { if (input.files.length) onFiles(input.files); });
}

// ── Knowledge base ───────────────────────────────────────

setupDragDrop('kbUploadZone', 'kbFileInput', files => {
  const el = $('kbFileNames');
  el.textContent = Array.from(files).map(f => f.name).join(', ');
  el.style.display = 'block';
  $('btnKbUpload').disabled = false;
});

$('btnKbUpload').addEventListener('click', async () => {
  const files = $('kbFileInput').files;
  if (!files.length) return;
  const loading = $('kbLoading');
  loading.classList.add('visible');
  $('btnKbUpload').disabled = true;
  const results = [];
  for (const file of files) {
    const form = new FormData();
    form.append('file', file);
    form.append('client_id', $('kbClientId').value);
    form.append('project_id', $('kbProjectId').value);
    form.append('solution_area', $('kbSolutionArea').value);
    form.append('share_with_groups', $('kbShareGroups').value);
    for (const [field, id] of [['source_type', 'kbSourceType'], ['approval_status', 'kbApproval'],
        ['effective_date', 'kbEffectiveDate'], ['version', 'kbVersion'], ['authority', 'kbAuthority']]) {
      if ($(id).value.trim()) form.append(field, $(id).value.trim());
    }
    try {
      const data = await fetchJson('/kb/upload', { method: 'POST', body: form });
      results.push('OK: ' + file.name + ' (' + data.indexed_chunks + ' chunks)');
    } catch (err) {
      results.push('FAIL: ' + file.name + ': ' + err.message);
    }
  }
  loading.classList.remove('visible');
  $('btnKbUpload').disabled = false;
  $('kbUploadResults').innerHTML =
    '<div class="result-box" style="margin-top:10px">' + results.map(esc).join('<br>') + '</div>';
  toast(results.length + ' file(s) processed');
  loadKbList();
});

async function loadKbList() {
  const list = $('kbDocList');
  try {
    const data = await fetchJson('/kb/list');
    if (!data.documents.length) {
      list.innerHTML = '<li style="color:var(--text-muted);font-size:12px;padding:10px">No documents indexed yet.</li>';
      return;
    }
    list.innerHTML = data.documents.map(doc => {
      const shared = doc.shared_with_groups && doc.shared_with_groups.length
        ? ' &middot; shared with ' + doc.shared_with_groups.length + ' group(s)'
        : ' &middot; private';
      const modified = doc.last_modified ? ' &middot; ' + esc(new Date(doc.last_modified).toLocaleDateString()) : '';
      const p = doc.provenance || {};
      const trust = p.source_type
        ? ' &middot; <span class="badge">' + esc(String(p.source_type).replace('_', ' ')) + '</span>' +
          (p.effective_date ? ' effective ' + esc(p.effective_date) : '') + (p.version ? ' v' + esc(p.version) : '')
        : '';
      const remove = doc.can_delete
        ? '<button class="btn btn-sm btn-danger" data-action="delete-kb" data-blob="' + esc(doc.blob_name) + '">Delete</button>'
        : '';
      return '<li class="kb-doc-item">' +
        '<div style="flex:1"><strong>' + esc(doc.filename) + '</strong>' +
        '<div class="doc-meta">' + esc(doc.client_id) + ' / ' + esc(doc.project_id) + ' / ' + esc(doc.solution_area) +
        ' &middot; ' + (num(doc.size_bytes) / 1024).toFixed(1) + ' KB' + modified + shared + trust + '</div></div>' +
        remove + '</li>';
    }).join('');
  } catch (err) {
    list.innerHTML = '<li style="color:var(--error)">Error: ' + esc(err.message) + '</li>';
  }
}
$('btnKbRefresh').addEventListener('click', loadKbList);

async function deleteKbDoc(blobName) {
  if (!confirm('Delete "' + blobName + '" and remove its indexed chunks?')) return;
  try {
    const data = await fetchJson('/kb/delete?blob_name=' + encodeURIComponent(blobName), { method: 'DELETE' });
    toast('Deleted: ' + data.deleted_chunks + ' chunks removed');
    loadKbList();
  } catch (err) { toast('Delete failed: ' + err.message, 'error'); }
}

$('btnKbReindex').addEventListener('click', async () => {
  if (!confirm('Re-index ALL documents in the knowledge base? This may take a while.')) return;
  const loading = $('reindexLoading');
  loading.classList.add('visible');
  $('btnKbReindex').disabled = true;
  try {
    const data = await fetchJson('/kb/reindex', { method: 'POST' });
    toast('Re-indexed ' + data.documents + ' docs, ' + data.total_chunks + ' chunks');
    loadKbList();
  } catch (err) { toast('Re-index failed: ' + err.message, 'error'); }
  finally { loading.classList.remove('visible'); $('btnKbReindex').disabled = false; }
});

// ── RFP: upload and extraction ───────────────────────────

setupDragDrop('rfpUploadZone', 'rfpFileInput', files => {
  $('rfpFileName').textContent = files[0].name + ' (' + (files[0].size / 1024).toFixed(1) + ' KB)';
  $('rfpFileName').style.display = 'block';
  $('btnRfpUpload').disabled = false;
});

$('btnRfpUpload').addEventListener('click', async () => {
  const file = $('rfpFileInput').files[0];
  if (!file) return;
  const loading = $('rfpUploadLoading');
  loading.classList.add('visible');
  $('btnRfpUpload').disabled = true;
  try {
    const form = new FormData();
    form.append('file', file);
    const data = await fetchJson('/rfp/upload', { method: 'POST', body: form });
    currentRfpId = data.rfp_id;
    currentFilename = data.filename;
    toast('RFP uploaded! ID: ' + currentRfpId);
    $('cardExtractPrompt').style.display = 'block';
    $('cardExtractPrompt').scrollIntoView({ behavior: 'smooth' });
  } catch (err) {
    toast('Upload failed: ' + err.message, 'error');
    $('btnRfpUpload').disabled = false;
  } finally { loading.classList.remove('visible'); }
});

$('btnExtract').addEventListener('click', async () => {
  const loading = $('extractLoading');
  const mode = document.querySelector('input[name="extractMode"]:checked').value;
  setLoadingText(loading, mode === 'ai' ? 'AI is extracting requirements...' : 'Parsing document locally...');
  loading.classList.add('visible');
  $('btnExtract').disabled = true;
  await extractReqs(mode);
  loading.classList.remove('visible');
});

function requirementBlock(req) {
  const category = req.category
    ? ' <span style="color:var(--text-muted);font-size:10px">[' + esc(req.category) + ']</span>' : '';
  const kind = req.type === 'question'
    ? '<span class="badge" style="margin-top:1px;background:#fff4ce;color:#8a6d00">question</span>' : '';
  const anchor = req.anchor
    ? '<span style="color:var(--text-muted);font-size:10px;white-space:nowrap" title="Where this is in the RFP">' + esc(req.anchor) + '</span>' : '';
  return '<div class="req-block" id="' + esc(blockDomId(req.id)) + '" style="border:1px solid var(--border);border-radius:8px;margin-bottom:10px;overflow:hidden">' +
    '<div style="padding:10px 12px;display:flex;gap:8px;align-items:flex-start;background:#fafafa;border-bottom:1px solid var(--border)">' +
      '<span style="font-weight:700;color:var(--primary);min-width:40px;font-size:12px">' + esc(req.id) + '</span>' +
      kind + '<span class="badge ' + priorityClass(req.priority) + '" style="margin-top:1px">' + esc(req.priority) + '</span>' + category +
      '<span style="flex:1;font-size:12px">' + esc(req.text) + '</span>' + anchor +
    '</div>' +
    '<div class="req-response" id="' + esc(respDomId(req.id)) + '" style="padding:10px 12px;font-size:12px;color:var(--text-muted);display:none">' +
      '<em>Response will appear here after draft generation...</em></div></div>';
}

async function extractReqs(mode) {
  $('cardExtract').style.display = 'block';
  $('cardExtract').scrollIntoView({ behavior: 'smooth' });
  try {
    const data = await postJson(mode === 'ai' ? '/rfp/extract-ai' : '/rfp/extract',
      { rfp_id: currentRfpId, filename: currentFilename });
    // Questions are requirements too (type "question"): each is drafted and gets a response slot.
    extractedRequirements = (data.requirements || []).map((r, i) => ({ ...r, id: normReqId(r.id || ('R' + (i + 1))) }));
    extractedQuestions = extractedRequirements.filter(r => r.type === 'question').map(r => r.text);
    extractedEvaluationCriteria = data.evaluation_criteria || [];

    $('extractSummary').innerHTML =
      '<div style="display:flex;gap:14px;font-size:12px;color:var(--text-muted)">' +
      '<span><strong>' + (data.sections?.length || 0) + '</strong> Sections</span>' +
      '<span><strong>' + (extractedRequirements.length - extractedQuestions.length) + '</strong> Requirements</span>' +
      '<span><strong>' + extractedQuestions.length + '</strong> Questions</span></div>';
    $('reqList').innerHTML = extractedRequirements.map(requirementBlock).join('');

    toast('Extracted ' + extractedRequirements.length + ' requirements');
    $('cardDraft').style.display = 'block';
    updateApproachLabel();
    if (templateSectionsData.length) renderTemplateSections(templateSectionsData);
  } catch (err) { toast('Extraction failed: ' + err.message, 'error'); }
}

// ── RFP: drafting ────────────────────────────────────────

function selectedApproach() {
  return document.querySelector('input[name="approach"]:checked')?.value || 'per_requirement';
}

function renderSingleResponse(resp) {
  const el = $(respDomId(resp?.requirement_id));
  if (!el) return;
  el.style.display = 'block';

  const meets = meetsLabel(resp.meets);
  const evidence = (resp.evidence || []).map(e => {
    const url = safeUrl(e.source_url);
    // A knowledge-base reference link opens the document in the app; for a PDF, at the page the evidence is on
    const target = url && e.page && new URL(url).pathname.startsWith('/kb/doc/') ? url + '#page=' + Number(e.page) : url;
    const title = esc(e.title || (url ? 'Source' : ''));
    const link = url
      ? '<a href="' + esc(target) + '" target="_blank" rel="noopener noreferrer" style="color:var(--primary)">' + title + '</a>'
      : title;
    const quote = e.quote ? ' &mdash; <em>"' + esc(String(e.quote).substring(0, 150)) + '"</em>' : '';
    const tag = e.source_type
      ? ' <span class="badge" style="font-size:10px" title="How far this source should be trusted">' +
        esc(String(e.source_type).replace('_', ' ')) + '</span>'
      : '';
    const where = [e.effective_date, e.version && ('v' + e.version), e.section, e.page && ('p. ' + e.page)]
      .filter(Boolean).map(esc).join(' &middot; ');
    const place = where ? ' <span style="color:var(--text-muted)">(' + where + ')</span>' : '';
    return '<li style="font-size:11px;padding:3px 0;border-bottom:1px solid #f0f0f0">' + link + tag + place + quote + '</li>';
  }).join('');
  const listed = (label, items, color) => (items || []).length
    ? '<div style="margin-top:4px"><strong style="font-size:11px">' + label + ':</strong> <span style="font-size:11px' +
      (color ? ';color:' + color : '') + '">' + items.map(esc).join('; ') + '</span></div>'
    : '';

  el.innerHTML =
    '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">' +
      '<span style="font-weight:700;font-size:13px" class="' + MEETS_CLASSES[meets] + '">' + esc(meets) + '</span>' +
      '<span style="font-size:11px;color:var(--text-muted)">Compliance</span></div>' +
    '<div style="font-size:12px;line-height:1.6;margin-bottom:6px">' + esc(resp.answer_markdown || '') + '</div>' +
    (evidence ? '<div style="margin-top:6px"><strong style="font-size:11px">Evidence:</strong><ul style="list-style:none;margin:4px 0 0 0;padding:0">' + evidence + '</ul></div>' : '') +
    (resp.kb_evidence_found === false ? '<div style="margin-top:6px;font-size:11px;color:#8a6d00">&#9888; No internal knowledge-base evidence was found for this requirement.</div>' : '') +
    listed('Assumptions', resp.assumptions) + listed('Risks', resp.risks, 'var(--error)');
  el.closest('.req-block')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function showProcessing(requirementId) {
  const el = $(respDomId(requirementId));
  if (el) { el.style.display = 'block'; el.innerHTML = SPINNER_HTML; }
}

/** The state of one streamed draft: progress counters and the section elements created for the map. */
function createStreamHandler(loading, approach) {
  let processed = 0, total = extractedRequirements.length;
  const sectionEls = new Map();

  return {
    get processed() { return processed; },
    handle(event) {
      switch (event.type) {
        case 'start':
          total = event.total;
          if (event.approach === 'per_section') {
            setLoadingText(loading, 'Step 1: Classifying requirements into template sections...');
          } else {
            setLoadingText(loading, 'Processing requirement 1 of ' + total + '...');
            if (extractedRequirements.length) showProcessing(extractedRequirements[0].id);
          }
          break;
        case 'phase':
          setLoadingText(loading, 'Phase ' + event.data.phase + ': ' + event.data.description + '...');
          break;
        case 'section_map': {
          setLoadingText(loading, 'Generating section narratives...');
          $('cardSectionNarratives').style.display = 'block';
          const list = $('sectionNarrativesList');
          list.innerHTML = '<div style="margin-bottom:12px;font-size:12px;color:var(--text-muted)">Requirements mapped to sections:</div>';
          sectionEls.clear();
          for (const [section, ids] of Object.entries(event.data)) {
            const box = document.createElement('div');
            box.style.cssText = 'padding:8px 12px;border:1px solid var(--border);border-radius:6px;margin-bottom:6px';
            box.innerHTML = '<strong style="font-size:13px">' + esc(section) + '</strong>' +
              '<span style="font-size:11px;color:var(--text-muted);margin-left:8px">' + ids.length +
              ' requirements: ' + ids.map(esc).join(', ') + '</span>';
            const content = document.createElement('div');
            content.style.cssText = 'margin-top:8px;display:none';
            box.appendChild(content);
            list.appendChild(box);
            sectionEls.set(section, content);
          }
          break;
        }
        case 'section_narrative': {
          const section = event.data;
          generatedSectionNarratives.push(section);
          updateDocGenStatus();
          setLoadingText(loading, 'Generated: ' + section.section_name);
          const content = sectionEls.get(section.section_name);
          if (content) {
            content.style.display = 'block';
            content.innerHTML = '<div style="font-size:12px;line-height:1.7;padding:8px;background:#fafafa;border-radius:4px">' +
              esc(section.narrative_markdown || '') + '</div>';
            content.parentElement.scrollIntoView({ behavior: 'smooth', block: 'center' });
          }
          break;
        }
        case 'requirement_started': {
          const progress = event.data || {};
          setLoadingText(loading, 'Compliance check ' + (progress.index || 0) + ' of ' + (progress.total || total) + '...');
          if (progress.requirement_id) showProcessing(progress.requirement_id);
          break;
        }
        case 'response':
          processed++;
          setLoadingText(loading, approach === 'per_section'
            ? 'Compliance check ' + processed + ' of ' + total + '...'
            : 'Processed ' + processed + ' of ' + total + ' requirements...');
          renderSingleResponse(event.data);
          generatedResponses.push(event.data);
          updateDocGenStatus();
          if (approach !== 'per_section' && processed < total && extractedRequirements[processed]) {
            showProcessing(extractedRequirements[processed].id);
          }
          break;
        case 'error':
          throw new Error(event.message || 'Unknown draft stream error');
        case 'complete':
          $('cardResults').style.display = 'block';
          if (event.outputs) {
            $('downloadLinks').style.display = 'flex';
            $('dlDocx').href = outputUrl(currentRfpId, 'draft.docx');
            $('dlXlsx').href = outputUrl(currentRfpId, 'compliance.xlsx');
          }
          $('draftMeta').textContent = processed + ' requirements processed (' + approach.replace('_', ' ') + ')';
          toast('Draft complete! ' + processed + ' responses generated.');
          break;
      }
    },
  };
}

// A draft runs as a background job on the server (a request would be cut off after 240 s): start it, then
// poll for the events it has produced since the last poll and feed them to the same handler as before.
const POLL_INTERVAL_MS = 2000, MAX_POLL_FAILURES = 5;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let activeJobId = null;

async function runDraftJob(body, stream) {
  const started = await postJson('/drafts', body);
  activeJobId = started.job_id;
  $('btnCancelDraft').style.display = 'inline-flex';

  let cursor = 0, failures = 0;
  for (;;) {
    let job;
    try {
      job = await fetchJson('/drafts/' + encodeURIComponent(activeJobId) + '?after=' + cursor);
      failures = 0;
    } catch (err) {
      // The draft keeps running on the server; a dropped poll (network blip, redeploy) is not a failure.
      if (++failures >= MAX_POLL_FAILURES) throw err;
      await sleep(POLL_INTERVAL_MS);
      continue;
    }
    for (const event of job.events) stream.handle(event);
    cursor = job.next;

    if (job.status === 'failed') throw new Error(job.error || 'The draft failed.');
    if (job.status === 'cancelled') { toast('Draft cancelled.', 'error'); return; }
    if (job.status === 'succeeded') return;
    await sleep(POLL_INTERVAL_MS);
  }
}

$('btnCancelDraft').addEventListener('click', async () => {
  if (!activeJobId) return;
  $('btnCancelDraft').disabled = true;
  try { await postJson('/drafts/' + encodeURIComponent(activeJobId) + '/cancel', {}); }
  catch (err) { toast('Could not cancel: ' + err.message, 'error'); $('btnCancelDraft').disabled = false; }
});

$('btnDraft').addEventListener('click', async () => {
  const loading = $('draftLoading'), button = $('btnDraft');
  loading.classList.add('visible');
  button.disabled = true;

  generatedResponses = [];
  generatedSectionNarratives = [];
  updateDocGenStatus();
  extractedRequirements.forEach(req => {
    const el = $(respDomId(req.id));
    if (el) { el.style.display = 'block'; el.innerHTML = '<em style="color:var(--text-muted)">Waiting to process...</em>'; }
  });

  const approach = selectedApproach();
  const stream = createStreamHandler(loading, approach);
  try {
    await runDraftJob({
      rfp_id: currentRfpId,
      client_id: $('draftClientId').value,
      project_id: $('draftProjectId').value,
      solution_area: $('draftSolutionArea').value,
      tone: $('draftTone').value,
      approach,
      context: $('draftContext').value.trim(),
      allow_web_search: $('draftWebSearch').checked,
      requirements: extractedRequirements,
    }, stream);
  } catch (err) {
    toast('Draft failed: ' + err.message, 'error');
  } finally {
    activeJobId = null;
    $('btnCancelDraft').style.display = 'none';
    $('btnCancelDraft').disabled = false;
    loading.classList.remove('visible');
    button.disabled = false;
  }
});

// ── Document generation ──────────────────────────────────

function updateDocGenStatus() {
  const el = $('docGenReqCount'), button = $('btnGenDoc');
  const approach = selectedApproach();
  const count = n => '<strong style="color:var(--success)">' + n + '</strong>';

  if (approach === 'per_section' && generatedSectionNarratives.length) {
    el.innerHTML = count(generatedSectionNarratives.length) + ' section narratives available.';
    button.disabled = false;
  } else if (generatedResponses.length) {
    el.innerHTML = count(generatedResponses.length) + ' requirement responses available.';
    button.disabled = false;
  } else {
    el.textContent = approach === 'per_section'
      ? 'No section narratives or requirement responses available yet.'
      : 'No requirement responses available yet.';
    button.disabled = true;
  }
}

$('btnGenDoc').addEventListener('click', async () => {
  const approach = selectedApproach();
  const hasSectionData = approach === 'per_section' && generatedSectionNarratives.length > 0;
  if (!generatedResponses.length && !hasSectionData) return toast('Generate responses or section narratives first.', 'error');
  if (!currentRfpId) return toast('No RFP uploaded.', 'error');

  const loading = $('docGenLoading'), button = $('btnGenDoc');
  loading.classList.add('visible');
  button.disabled = true;
  try {
    const data = await postJson('/draft/generate-doc', {
      rfp_id: currentRfpId,
      responses: generatedResponses,
      section_narratives: generatedSectionNarratives,
      approach,
      client_id: $('draftClientId').value,
      project_id: $('draftProjectId').value,
      solution_area: $('draftSolutionArea').value,
      tone: $('draftTone').value || 'professional',
    });

    if (data.sections?.length) {
      const box = $('docGenSections');
      box.style.display = 'block';
      box.innerHTML = '<h3>Document Sections</h3>' + data.sections.map(s =>
        '<div style="border:1px solid var(--border);border-radius:6px;padding:10px;margin-bottom:8px">' +
          '<strong>' + esc(s.section_name) + '</strong>' +
          '<span style="font-size:11px;color:var(--text-muted);margin-left:8px">' + (s.requirement_ids?.length || 0) + ' requirements</span>' +
          '<div style="font-size:12px;line-height:1.6;margin-top:6px;background:#fafafa;padding:8px;border-radius:4px">' + esc(s.narrative_markdown || '') + '</div>' +
        '</div>').join('');
    }
    if (data.outputs) {
      $('docGenDownloads').style.display = 'flex';
      $('docGenDocx').href = outputUrl(currentRfpId, 'draft.docx');
      $('docGenXlsx').href = outputUrl(currentRfpId, 'compliance.xlsx');
    }
    toast('Document generated! ' + (data.sections?.length || 0) + ' sections.');
  } catch (err) {
    toast('Document generation failed: ' + err.message, 'error');
  } finally {
    loading.classList.remove('visible');
    button.disabled = false;
  }
});

// ── Template ─────────────────────────────────────────────

function updateApproachLabel() {
  const names = { per_requirement: 'Per Requirement', per_section: 'Per Section', map_reduce: 'Map-Reduce' };
  const approach = selectedApproach();
  $('currentApproachLabel').textContent = names[approach] || approach;
  updateDocGenStatus();
}
document.addEventListener('change', e => { if (e.target.name === 'approach') updateApproachLabel(); });

function setApproachNotes(html, color) {
  for (const id of ['sectionApproachNote', 'mapReduceNote']) { $(id).innerHTML = html; $(id).style.color = color; }
}

async function loadTemplateInfo() {
  const el = $('templateInfo');
  try {
    const data = await fetchJson('/template/info');
    if (data.exists) {
      el.textContent = 'Template loaded: ' + (num(data.size_bytes) / 1024).toFixed(1) + ' KB, last modified: ' +
        new Date(data.last_modified).toLocaleString();
      el.style.borderColor = '#a3d977';
      renderTemplateSections(data.sections || []);
      if (data.sections?.length) setApproachNotes('<br>&#10003; Template: ' + num(data.sections.length) + ' sections.', 'var(--success)');
    } else {
      el.textContent = 'No custom template. A default will be used.';
      el.style.borderColor = '#ffe082';
      $('cardTplSections').style.display = 'none';
      setApproachNotes('<br>&#9888; No template sections. Defaults will be used.', '#8a6d00');
    }
  } catch { el.textContent = 'Could not check template status.'; }
}

function renderTemplateSections(sections) {
  templateSectionsData = sections;
  const card = $('cardTplSections');
  if (!sections.length) { card.style.display = 'none'; return; }
  card.style.display = 'block';
  $('tplSectionsList').innerHTML = sections.map((s, i) => {
    const indent = Math.max(0, num(s.level) - 1) * 16;
    const placeholder = s.placeholder
      ? ' <span style="background:#fff4ce;color:#8a6d00;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600">{{' + esc(s.placeholder) + '}}</span>'
      : '';
    return '<div style="border:1px solid var(--border);border-radius:8px;margin-bottom:10px;margin-left:' + indent + 'px;overflow:hidden" id="tpl-section-' + i + '">' +
      '<div style="padding:10px 12px;background:#fafafa;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px">' +
        '<span style="background:var(--primary);color:#fff;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600">H' + num(s.level) + '</span>' +
        '<strong style="font-size:13px;flex:1">' + esc(s.heading) + '</strong>' + placeholder +
        '<button class="btn btn-primary btn-sm" data-action="generate-section" data-index="' + i + '">Generate</button>' +
      '</div>' +
      '<div style="padding:10px 12px">' +
        '<label style="font-size:11px;margin-bottom:4px">Prompt for this section:</label>' +
        '<textarea id="tpl-prompt-' + i + '" rows="3" style="width:100%;font-size:11px;padding:6px 8px;border:1px solid var(--border);border-radius:4px;font-family:inherit;margin-bottom:6px">' + esc(buildDefaultPrompt(s)) + '</textarea>' +
        '<div id="tpl-response-' + i + '" style="display:none">' +
          '<label style="font-size:11px;color:var(--text-muted);margin-bottom:4px">Generated response:</label>' +
          '<div id="tpl-response-text-' + i + '" style="font-size:12px;line-height:1.6;background:#f5f9ff;padding:10px;border-radius:4px;border:1px solid #d0e4ff;max-height:300px;overflow-y:auto"></div>' +
        '</div>' +
        '<div id="tpl-loading-' + i + '" class="loading"><div class="spinner"></div><span>Generating...</span></div>' +
      '</div></div>';
  }).join('');
}

// Suggests a prompt for a section from the most relevant extracted requirements and questions.
const STOP_WORDS = new Set(['the', 'and', 'for', 'with', 'from', 'that', 'this', 'into', 'your', 'you', 'are', 'our', 'all', 'any', 'not', 'but', 'use', 'using', 'each', 'per', 'section', 'response', 'rfp', 'document']);

function tokenizeForPromptCoach(text) {
  return new Set((text || '').toLowerCase().replace(/[^a-z0-9\s]/g, ' ').split(/\s+/)
    .filter(w => w.length > 2 && !STOP_WORDS.has(w)));
}
function promptCoachScore(text, keywords) {
  const words = tokenizeForPromptCoach(text);
  let score = 0;
  keywords.forEach(k => { if (words.has(k)) score += 1; });
  return score;
}
function rankRelevantRequirements(keywords, requirements) {
  return (requirements || [])
    .map(r => {
      let score = promptCoachScore((r.text || '') + ' ' + (r.category || '') + ' ' + (r.priority || ''), keywords);
      if ((r.priority || '').toLowerCase() === 'mandatory') score += 0.5;
      return { ...r, _score: score };
    })
    .sort((a, b) => b._score - a._score || ((a.id || '').localeCompare(b.id || '')));
}
function rankRelevantTexts(keywords, items) {
  return (items || []).map(t => ({ text: t, _score: promptCoachScore(t, keywords) }))
    .sort((a, b) => b._score - a._score).map(x => x.text);
}
function truncate(text, maxLen) {
  if (!text) return '';
  return text.length > maxLen ? text.substring(0, maxLen - 3) + '...' : text;
}

function buildDefaultPrompt(section) {
  const name = section.heading;
  const hasRfpContext = extractedRequirements.length > 0 || extractedQuestions.length > 0;
  if (!hasRfpContext) {
    if (generatedResponses.length) {
      return 'Write the "' + name + '" section of a professional RFP response document.\n' +
        'Use the requirement responses provided as source material.\n' +
        'Synthesize them into a coherent, flowing narrative, not a list.\n' +
        'Reference requirement IDs in brackets where relevant, e.g. [F1].\n' +
        'Tone: professional.';
    }
    return 'Write the "' + name + '" section of a professional RFP response document.\n' +
      'This section should cover the key points relevant to "' + name + '".\n' +
      'Use only the context provided; do not invent capabilities.\n' +
      'Tone: professional.';
  }

  const keywords = tokenizeForPromptCoach(name + ' ' + (section.placeholder || ''));
  const requirements = rankRelevantRequirements(keywords, extractedRequirements.filter(r => r.type !== 'question')).slice(0, 10);
  const questions = rankRelevantTexts(keywords, extractedQuestions).slice(0, 5);
  const criteria = rankRelevantTexts(keywords, extractedEvaluationCriteria).slice(0, 5);
  const tone = $('draftTone')?.value || 'professional';
  const commonContext = $('draftContext')?.value?.trim() || '';

  const requirementLines = requirements.length
    ? requirements.map(r => '- [' + (r.id || '?') + '] ' + truncate(r.text || '', 200)).join('\n')
    : '- No highly-matched requirements found for this section. Use overall RFP context.';
  const lines = items => (items.length ? items.map(t => '- ' + truncate(t, 180)).join('\n') : '- None');

  return 'You are drafting the "' + name + '" section of an RFP response.\n' +
    'Use ONLY the RFP-specific context below as primary source material.\n\n' +
    (commonContext ? 'Company context:\n' + commonContext + '\n\n' : '') +
    'Section objective:\n' +
    '- Address buyer intent for "' + name + '" directly and concretely.\n' +
    '- Prioritize mandatory and technical/security constraints where relevant.\n' +
    '- Keep claims aligned to the listed requirements and questions.\n\n' +
    'Most relevant extracted requirements:\n' + requirementLines + '\n\n' +
    'Relevant extracted questions:\n' + lines(questions) + '\n\n' +
    'Relevant evaluation criteria:\n' + lines(criteria) + '\n\n' +
    'Writing instructions:\n' +
    '- Produce a coherent section narrative, not bullet-only output.\n' +
    '- Explicitly cover how we satisfy key requirements.\n' +
    '- Call out assumptions/risks only when necessary.\n' +
    '- Reference requirement IDs in brackets where useful, e.g. [R12].\n' +
    '- Tone: ' + tone + '.\n' +
    '- Do not invent capabilities not supported by provided context.';
}

async function generateSection(index) {
  const section = templateSectionsData[index];
  if (!section) return;
  const loading = $('tpl-loading-' + index);
  loading.classList.add('visible');
  try {
    const data = await postJson('/draft/generate-section', {
      section_name: section.heading,
      prompt: $('tpl-prompt-' + index)?.value || '',
      responses: generatedResponses,
      tone: $('draftTone').value || 'professional',
    });
    const text = data.narrative_markdown || data.raw_response || '';
    const payload = {
      section_name: data.section_name || section.heading,
      narrative_markdown: text,
      requirement_ids: data.requirement_ids || [],
    };
    const existing = generatedSectionNarratives.findIndex(s => s.section_name === payload.section_name);
    if (existing >= 0) generatedSectionNarratives[existing] = payload; else generatedSectionNarratives.push(payload);
    updateDocGenStatus();
    $('tpl-response-' + index).style.display = 'block';
    $('tpl-response-text-' + index).textContent = text;
    toast('Section "' + section.heading + '" generated.');
  } catch (err) {
    toast('Failed: ' + err.message, 'error');
  } finally { loading.classList.remove('visible'); }
}

async function generateAllSections() {
  for (let i = 0; i < templateSectionsData.length; i++) await generateSection(i);
  toast('All sections generated!');
}
$('btnGenAllSections').addEventListener('click', generateAllSections);

setupDragDrop('tplUploadZone', 'tplFileInput', files => {
  $('tplFileName').textContent = files[0].name;
  $('tplFileName').style.display = 'block';
  $('btnTplUpload').disabled = false;
});

$('btnTplUpload').addEventListener('click', async () => {
  const file = $('tplFileInput').files[0];
  if (!file) return;
  const loading = $('tplLoading');
  loading.classList.add('visible');
  $('btnTplUpload').disabled = true;
  try {
    const form = new FormData();
    form.append('file', file);
    const data = await fetchJson('/template/upload', { method: 'POST', body: form });
    if (data.warning) {
      toast(data.warning, 'error');
      $('templateInfo').innerHTML = '<span style="color:var(--error)">&#9888; ' + esc(data.warning) + '</span>';
      $('templateInfo').style.borderColor = 'var(--error)';
    } else {
      toast('Template uploaded! ' + (data.sections?.length || 0) + ' sections detected.');
    }
    renderTemplateSections(data.sections || []);
    loadTemplateInfo();
  } catch (err) { toast('Upload failed: ' + err.message, 'error'); }
  finally { loading.classList.remove('visible'); $('btnTplUpload').disabled = false; }
});

// ── Ask ──────────────────────────────────────────────────

$('btnAsk').addEventListener('click', async () => {
  const question = $('askQuestion').value.trim();
  if (!question) return toast('Enter a question', 'error');
  const loading = $('askLoading'), button = $('btnAsk');
  loading.classList.add('visible');
  button.disabled = true;
  try {
    const data = await postJson('/answer', {
      question,
      client_id: $('askClientId').value,
      project_id: $('askProjectId').value,
      solution_area: $('askSolutionArea').value,
      allow_web_search: $('askWebSearch').checked,
    });
    $('answerBox').style.display = 'block';
    $('answerText').textContent = data.answer_markdown || data.raw_response || JSON.stringify(data);
    $('answerJson').textContent = JSON.stringify(data, null, 2);
    toast('Answer received!');
  } catch (err) { toast('Error: ' + err.message, 'error'); }
  finally { loading.classList.remove('visible'); button.disabled = false; }
});

// ── Dashboard ────────────────────────────────────────────

async function loadDashboard() {
  try {
    const [health, metrics] = await Promise.all([fetchJson('/health'), fetchJson('/metrics')]);
    $('healthResult').textContent = JSON.stringify(health, null, 2);
    $('healthResult').style.whiteSpace = 'pre-wrap';
    const labels = { rfp_uploads: 'RFP Uploads', extractions: 'Extractions', drafts: 'Drafts', answers: 'Answers', redaction_blocks: 'Redactions' };
    $('metricsGrid').innerHTML = Object.entries(labels).map(([key, label]) =>
      '<div class="metric-card"><div class="value">' + num(metrics[key]) + '</div><div class="label">' + esc(label) + '</div></div>').join('');
  } catch (err) {
    $('healthResult').textContent = err.message;
    $('healthResult').style.color = 'var(--error)';
  }
}
$('btnRefreshMetrics').addEventListener('click', loadDashboard);

// ── Live logs ────────────────────────────────────────────

let logWs = null, logEntries = [];
const LOG_LEVELS = new Set(['INFO', 'WARNING', 'ERROR', 'DEBUG', 'CRITICAL']);

function connectLogWs() {
  if (logWs && logWs.readyState <= 1) return;
  logWs = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/logs');
  logWs.onopen = () => { $('wsDot').classList.add('connected'); $('wsStatus').textContent = 'Connected'; };
  logWs.onclose = () => {
    $('wsDot').classList.remove('connected');
    $('wsStatus').textContent = 'Disconnected';
    setTimeout(() => { if ($('tab-logs').classList.contains('active')) connectLogWs(); }, 3000);
  };
  logWs.onerror = () => { $('wsStatus').textContent = 'Error'; };
  logWs.onmessage = evt => {
    try {
      const entry = JSON.parse(evt.data);
      logEntries.push(entry);
      if (logEntries.length > 2000) logEntries = logEntries.slice(-1500);
      renderLogEntry(entry);
      $('logCounter').textContent = logEntries.length;
    } catch { /* ignore malformed entries */ }
  };
}

function renderLogEntry(entry) {
  const filter = $('logFilter').value.toLowerCase();
  const message = String(entry.message ?? '');
  if (filter && !message.toLowerCase().includes(filter) && !String(entry.logger || '').toLowerCase().includes(filter)) return;
  const scroll = $('logScroll');
  const row = document.createElement('div');
  row.className = 'log-entry';
  const time = entry.ts ? (String(entry.ts).split('T')[1] || '').replace('Z', '') : '';
  const level = LOG_LEVELS.has(entry.level) ? entry.level : 'INFO';
  row.innerHTML = '<span class="log-ts">' + esc(time) + '</span><span class="log-level ' + level + '">' + level + '</span><span class="log-msg">' + esc(message) + '</span>';
  scroll.appendChild(row);
  if ($('logAutoScroll').checked) scroll.scrollTop = scroll.scrollHeight;
}
$('logFilter').addEventListener('input', () => { $('logScroll').innerHTML = ''; logEntries.forEach(renderLogEntry); });
$('btnClearLogs').addEventListener('click', () => { logEntries = []; $('logScroll').innerHTML = ''; $('logCounter').textContent = '0'; });
connectLogWs();

// ── Buttons created by the code above ────────────────────

document.addEventListener('click', e => {
  const button = e.target.closest('[data-action]');
  if (!button) return;
  if (button.dataset.action === 'delete-kb') deleteKbDoc(button.dataset.blob);
  if (button.dataset.action === 'generate-section') generateSection(Number(button.dataset.index));
});
