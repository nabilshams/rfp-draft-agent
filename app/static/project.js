'use strict';
/* Projects: the ledger-backed workflow. A project is kept on the server (requirements, versioned answers, history),
 * so reloading the page or opening it on another machine restores everything, and several people can work on it.
 *
 * Without a ledger on the server (the endpoints answer 503) none of this is used and the page works as before, with
 * its state in the browser. Uses the helpers and globals of app.js; the HTML comes from render.js. */

let projectMode = false;       // the server has a ledger
let currentProject = null;     // { id, name, rfp_id, can_manage }
let projectReqs = new Map();   // rid -> requirement, as last loaded
let projectRunning = false;    // a draft is running (started by anyone)
let projectWatching = false;   // this page is following a running draft

const PROJECT_REFRESH_MS = 15000;

const patchJson = (path, body) =>
  fetchJson(path, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const projectPath = suffix => '/projects/' + encodeURIComponent(currentProject.id) + (suffix || '');
const requirementPath = (rid, suffix) => projectPath('/requirements/' + encodeURIComponent(rid) + (suffix || ''));

// ── Starting up ──────────────────────────────────────────

async function initProjects() {
  let list;
  try {
    list = await fetchJson('/projects');
  } catch (err) {
    console.info('Projects are not available on this server; working in the browser only.', err.message);
    return;
  }
  projectMode = true;
  $('cardProjects').style.display = 'block';
  $('btnExtract').textContent = 'Create project';
  $('projectCreateRow').style.display = 'block';
  $('projectDraftNote').style.display = 'block';
  $('btnDraft').textContent = 'Draft unanswered requirements';
  renderProjectList(list.projects);

  const id = new URLSearchParams(location.hash.slice(1)).get('project');
  if (id) await openProject(id);
}

function renderProjectList(projects) {
  $('projectList').innerHTML = projectListHtml(projects);
}

async function loadProjectList() {
  try { renderProjectList((await fetchJson('/projects')).projects); }
  catch (err) { toast('Could not load your projects: ' + err.message, 'error'); }
}

// ── Opening and showing a project ────────────────────────

async function openProject(id) {
  try {
    showProject(await fetchJson('/projects/' + encodeURIComponent(id)));
    await refreshProject(true);
  } catch (err) {
    toast('Could not open the project: ' + err.message, 'error');
    history.replaceState(null, '', location.pathname);
  }
}

function showProject(state) {
  currentProject = state.project;
  currentRfpId = state.project.rfp_id || currentRfpId;
  history.replaceState(null, '', '#project=' + encodeURIComponent(state.project.id));
  projectReqs = new Map(state.requirements.map(r => [r.rid, r]));

  $('extractTitle').textContent = 'Project: ' + state.project.name;
  $('projectBar').style.display = 'flex';
  $('btnDeleteProject').style.display = state.project.can_manage ? 'inline-flex' : 'none';
  $('cardExtract').style.display = 'block';
  $('cardDraft').style.display = 'block';
  renderAllRequirements();
  syncPageState();
  updateApproachLabel();
  $('cardExtract').scrollIntoView({ behavior: 'smooth' });
}

function renderAllRequirements() {
  $('reqList').innerHTML = [...projectReqs.values()].map(r => projectRequirementHtml(r, projectRunning)).join('');
}

/** Counts, the draft button and the data the older document-generation step reads. */
function syncPageState() {
  const reqs = [...projectReqs.values()];
  const counts = { extracted: 0, drafted: 0, edited: 0, approved: 0, needs_input: 0, flagged: 0, not_answered: 0, repeats: 0 };
  reqs.forEach(r => {
    if (!isAnswered(r)) counts.not_answered += 1; // instructions, dates and rules are not answered
    else if (isRepeat(r) && !r.answer) counts.repeats += 1; // a repeat takes the first one's answer
    else counts[r.status] = (counts[r.status] || 0) + 1;
  });
  counts.flagged = reqs.filter(r => r.verification === 'flagged').length; // not a status: it overlaps with them
  counts.low_confidence = reqs.filter(r => r.confidence === 'low' && r.answer).length;
  $('extractSummary').innerHTML = countsHtml(counts, reqs.length);
  const remaining = counts.extracted;
  const unconfirmed = Boolean(currentProject && currentProject.extraction && currentProject.extraction.confirmed === false);
  $('btnDraft').textContent = unconfirmed ? 'Confirm the requirements first'
    : remaining ? 'Draft unanswered requirements (' + remaining + ')' : 'Everything has an answer';
  $('btnDraft').disabled = projectRunning || remaining === 0 || unconfirmed;
  $('projectRecord').innerHTML = projectRecordHtml(currentProject);

  extractedRequirements = reqs.map(r => ({ id: r.rid, text: r.text, category: r.category, priority: r.priority, type: r.type }));
  extractedQuestions = reqs.filter(r => r.type === 'question').map(r => r.text);
  generatedResponses = reqs.filter(r => r.answer).map(r => ({
    requirement_id: r.rid, requirement_text: r.text, answer_markdown: r.answer.answer_markdown, meets: r.answer.meets,
    evidence: r.answer.evidence, assumptions: r.answer.assumptions, risks: r.answer.risks, followups: r.answer.followups,
    kb_evidence_found: r.answer.kb_evidence_found, state: r.state, missing: r.missing,
    claims: r.answer.claims, verification: r.answer.verification, confidence: r.answer.confidence,
  }));
  updateDocGenStatus();
}

function openEditForm(rid) {
  const block = document.getElementById(blockDomId(rid));
  return block ? block.querySelector('.edit-form') : null;
}

function replaceBlock(req) {
  const old = document.getElementById(blockDomId(req.rid));
  const template = document.createElement('template');
  template.innerHTML = projectRequirementHtml(req, projectRunning);
  if (old) old.replaceWith(template.content.firstElementChild);
}

/** Show newer state without disturbing anybody who is typing: a block with an open edit form is left alone. */
function applyState(state, running) {
  const runningChanged = running !== projectRunning;
  projectRunning = running;
  const seen = new Set();
  for (const req of state.requirements) {
    seen.add(req.rid);
    const before = projectReqs.get(req.rid);
    projectReqs.set(req.rid, req);
    const changed = !before || before.rev !== req.rev || runningChanged;
    if (changed && !openEditForm(req.rid)) replaceBlock(req);
  }
  for (const rid of [...projectReqs.keys()]) if (!seen.has(rid)) projectReqs.delete(rid);
  currentProject = { ...currentProject, ...state.project };
  $('extractTitle').textContent = 'Project: ' + currentProject.name;
  syncPageState();
}

async function refreshProject(quiet) {
  if (!currentProject) return;
  try {
    const [state, status] = await Promise.all([fetchJson(projectPath()), fetchJson(projectPath('/draft'))]);
    applyState(state, status.running);
    showDownloads(status);
    if (status.running && !projectWatching) watchDraft();
    if (status.interrupted && !quiet) toast('The last draft was interrupted. Draft again to carry on.', 'error');
  } catch (err) {
    if (!quiet) toast('Could not refresh: ' + err.message, 'error');
  }
}

function showDownloads(status) {
  const downloads = status.job && status.job.downloads;
  $('cardResults').style.display = downloads ? 'block' : $('cardResults').style.display;
  if (downloads) {
    $('downloadLinks').style.display = 'flex';
    $('dlDocx').href = outputUrl(currentProject.id, 'draft.docx');
    $('dlXlsx').href = outputUrl(currentProject.id, 'compliance.xlsx');
    $('draftMeta').textContent = 'Documents built from every answer in this project.';
  }
}

setInterval(() => {
  if (currentProject && !projectWatching && !document.hidden) refreshProject(true);
}, PROJECT_REFRESH_MS);

// ── Creating a project from the uploaded RFP ─────────────

async function createProjectFromRfp(mode) {
  const groups = $('projectGroups').value.split(',').map(g => g.trim()).filter(Boolean);
  const body = {
    name: $('projectName').value.trim() || currentFilename || 'RFP response',
    rfp_id: currentRfpId,
    filename: currentFilename,
    share_with_groups: groups,
  };
  try {
    // The server reads the RFP, so the dates, submission rules and how it was read are kept with the project
    if (mode === 'ai') body.extractor = 'ai';
    showProject(await postJson('/projects', body));
    toast('Project created with ' + projectReqs.size + ' requirements.');
    loadProjectList();
  } catch (err) {
    toast('Could not create the project: ' + err.message, 'error');
  } finally {
    $('btnExtract').disabled = false;
  }
}

// ── Drafting ─────────────────────────────────────────────

const draftSettings = () => ({
  client_id: $('draftClientId').value,
  kb_project_id: $('draftProjectId').value,
  solution_area: $('draftSolutionArea').value,
  tone: $('draftTone').value,
  approach: selectedApproach(),
  context: $('draftContext').value.trim(),
  allow_web_search: $('draftWebSearch').checked,
});

async function startProjectDraft(rids) {
  try {
    await postJson(projectPath('/draft'), { ...draftSettings(), ...(rids ? { rids } : {}) });
  } catch (err) {
    toast('Could not start the draft: ' + err.message, 'error');
    return;
  }
  projectRunning = true;
  renderAllRequirementsKeepingEdits();
  syncPageState();
  watchDraft();
}

function renderAllRequirementsKeepingEdits() {
  for (const req of projectReqs.values()) if (!openEditForm(req.rid)) replaceBlock(req);
}

function progressText(status) {
  const job = status.job;
  if (!job) return 'Starting the draft...';
  return 'Drafting: ' + job.processed + ' of ' + job.total + ' requirements done. Each answer is saved as it arrives.';
}

async function watchDraft() {
  if (projectWatching) return;
  projectWatching = true;
  const loading = $('draftLoading');
  loading.classList.add('visible');
  $('btnCancelDraft').style.display = 'inline-flex';
  try {
    let failures = 0, status = null;
    for (;;) {
      try {
        status = await fetchJson(projectPath('/draft'));
        applyState(await fetchJson(projectPath()), status.running);
        failures = 0;
      } catch (err) {
        // The draft keeps running on the server; a dropped poll is not a failure.
        if (++failures >= MAX_POLL_FAILURES) throw err;
        await sleep(POLL_INTERVAL_MS);
        continue;
      }
      setLoadingText(loading, progressText(status));
      showDownloads(status);
      if (!status.running) break;
      await sleep(POLL_INTERVAL_MS);
    }
    reportDraftEnd(status);
  } catch (err) {
    toast('Lost contact with the draft: ' + err.message, 'error');
  } finally {
    projectWatching = false;
    loading.classList.remove('visible');
    $('btnCancelDraft').style.display = 'none';
    $('btnCancelDraft').disabled = false;
    syncPageState();
  }
}

function reportDraftEnd(status) {
  const job = status.job || {};
  if (status.interrupted) toast('The draft was interrupted. Answers already saved are kept; draft again to carry on.', 'error');
  else if (job.status === 'failed') toast('The draft stopped: ' + (job.error || 'it failed') + ' Answers already saved are kept.', 'error');
  else if (job.status === 'cancelled') toast('Draft cancelled. Answers already saved are kept.', 'error');
  else toast('Draft complete.' + (status.remaining ? ' ' + status.remaining + ' requirements still have no answer.' : ''));
}

async function cancelProjectDraft() {
  $('btnCancelDraft').disabled = true;
  try { await postJson(projectPath('/draft/cancel'), {}); }
  catch (err) { toast('Could not cancel: ' + err.message, 'error'); $('btnCancelDraft').disabled = false; }
}

// ── Editing, approving, versions and history ─────────────

function extraArea(rid) {
  const block = document.getElementById(blockDomId(rid));
  return block ? block.querySelector('.req-extra') : null;
}

async function saveAnswer(button) {
  const rid = button.dataset.rid, block = button.closest('.req-block');
  const text = block.querySelector('.edit-text').value.trim();
  if (!text) return toast('Write an answer first.', 'error');
  await changeRequirement(() => postJson(requirementPath(rid, '/answers'), {
    answer_markdown: text, meets: block.querySelector('.edit-meets').value || undefined, expected_rev: Number(button.dataset.rev),
  }), 'Saved as a new version.');
}

async function changeRequirement(request, successMessage) {
  try {
    const updated = await request();
    projectReqs.set(updated.rid, updated);
    replaceBlock(updated);
    syncPageState();
    toast(successMessage);
  } catch (err) {
    toast(err.message, 'error'); // a 409 says somebody else changed it: the page then shows their version
    await refreshProject(true);
  }
}

document.addEventListener('click', async e => {
  const button = e.target.closest('[data-action]');
  if (!button || !projectMode) return;
  const rid = button.dataset.rid, req = projectReqs.get(rid);

  switch (button.dataset.action) {
    case 'open-project': return openProject(button.dataset.project);
    case 'edit-answer': {
      const area = extraArea(rid);
      if (area && req) area.innerHTML = editFormHtml(req);
      return;
    }
    case 'close-extra': {
      const area = extraArea(rid);
      if (area) area.innerHTML = '';
      return;
    }
    case 'save-answer': return saveAnswer(button);
    case 'edit-type': {
      const area = extraArea(rid);
      if (area && req) area.innerHTML = itemTypeFormHtml(req);
      return;
    }
    case 'save-type': {
      const block = button.closest('.req-block');
      const changes = {
        item_kind: block.querySelector('.type-kind').value,
        response_type: block.querySelector('.type-response').value,
        evidence_required: block.querySelector('.type-evidence').checked,
        duplicate_of: block.querySelector('.type-repeat').value.trim(),
        expected_rev: Number(button.dataset.rev),
      };
      return changeRequirement(async () => {
        const updated = await patchJson(requirementPath(rid), changes);
        await refreshProject(true); // moving an item moves its repeats too
        return updated;
      }, 'Saved.');
    }
    case 'ungroup':
      return changeRequirement(async () => {
        const updated = await patchJson(requirementPath(rid), { duplicate_of: '', expected_rev: Number(button.dataset.rev) });
        await refreshProject(true);
        return updated;
      }, 'It will be answered on its own.');
    case 'confirm-extraction': {
      try {
        const project = await postJson(projectPath('/confirm-extraction'), {});
        currentProject = { ...currentProject, ...project };
        syncPageState();
        toast('Confirmed. You can draft now.');
      } catch (err) { toast('Could not confirm: ' + err.message, 'error'); }
      return;
    }
    case 'approve':
      return changeRequirement(() => patchJson(requirementPath(rid), { status: 'approved', expected_rev: req.rev }), 'Approved.');
    case 'restore':
      return changeRequirement(() => postJson(requirementPath(rid, '/answers'),
        { restore_version: Number(button.dataset.version), expected_rev: Number(button.dataset.rev) }), 'Restored as a new version.');
    case 'versions': {
      const area = extraArea(rid);
      try {
        const data = await fetchJson(requirementPath(rid, '/answers'));
        if (area) area.innerHTML = versionsHtml(rid, data.answers, req.answer_version, req.rev);
      } catch (err) { toast('Could not load the versions: ' + err.message, 'error'); }
      return;
    }
    case 'history': {
      const area = extraArea(rid);
      try {
        const data = await fetchJson(projectPath('/events?limit=50&rid=' + encodeURIComponent(rid)));
        if (area) area.innerHTML = eventsHtml(data.events, rid);
      } catch (err) { toast('Could not load the history: ' + err.message, 'error'); }
      return;
    }
    case 'redraft':
      if (req && req.answer && !confirm('Draft ' + rid + ' again? The current answer is kept as an earlier version.')) return;
      return startProjectDraft([rid]);
  }
});

// ── The project's own buttons ────────────────────────────

$('btnProjectRefresh').addEventListener('click', () => refreshProject(false));
$('btnProjectActivity').addEventListener('click', async () => {
  try {
    const data = await fetchJson(projectPath('/events?limit=30'));
    $('projectActivity').innerHTML = eventsHtml(data.events, null);
  } catch (err) { toast('Could not load the activity: ' + err.message, 'error'); }
});
$('btnProjectBack').addEventListener('click', () => {
  currentProject = null;
  history.replaceState(null, '', location.pathname);
  ['cardExtract', 'cardDraft', 'cardResults', 'cardSectionNarratives'].forEach(id => { $(id).style.display = 'none'; });
  $('projectBar').style.display = 'none';
  $('projectActivity').innerHTML = '';
  loadProjectList();
  window.scrollTo({ top: 0, behavior: 'smooth' });
});
$('btnDeleteProject').addEventListener('click', async () => {
  if (!currentProject || !confirm('Delete "' + currentProject.name + '" with all its answers and history? This cannot be undone.')) return;
  try {
    await fetchJson(projectPath(), { method: 'DELETE' });
    toast('Project deleted.');
    $('btnProjectBack').click();
  } catch (err) { toast('Could not delete the project: ' + err.message, 'error'); }
});

initProjects();
