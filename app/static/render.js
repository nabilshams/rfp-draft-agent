'use strict';
/* HTML builders for requirements, answers and project history. Pure functions from data to an HTML string, so they
 * can be unit tested with Node (see tests/test_ui_projects.py). Every piece of text goes through esc(), every link
 * through safeUrl(): see the note at the top of app.js. In the browser esc and safeUrl are globals from safe.js. */

if (typeof module !== 'undefined') { var { esc, safeUrl } = require('./safe.js'); } // eslint-disable-line no-var

const MEETS_CLASSES = { Yes: 'meets-yes', Partial: 'meets-partial', No: 'meets-no' };
// A requirement waiting for a person makes no compliance claim: its `meets` is empty, which is not a "No".
const meetsLabel = meets => (meets in MEETS_CLASSES ? meets : (meets ? 'No' : ''));
const PRIORITY_CLASSES = { mandatory: 'badge-mandatory', preferred: 'badge-preferred' };
const priorityClass = priority => PRIORITY_CLASSES[priority] || 'badge-standard';

const STATUS_LABELS = {
  extracted: 'No answer yet', drafted: 'Drafted', edited: 'Edited', approved: 'Approved', needs_input: 'Needs input',
};
const STATUS_STYLES = {
  extracted: 'background:#f3f2f1;color:#605e5c',
  drafted: 'background:#e6f0ff;color:#0f6cbd',
  edited: 'background:#fff4ce;color:#8a6d00',
  approved: 'background:#e8f5e9;color:var(--success)',
  needs_input: 'background:#fde7e9;color:#a4262c',
};

// How far the sources answered a requirement (see app/agents/states.py). Placeholders are for a person to fill in.
const STATE_LABELS = {
  fully_answered: 'Fully answered', partially_answered: 'Partially answered', conflicting_evidence: 'Conflicting evidence',
  no_evidence: 'No evidence', needs_sme: 'Needs SME', needs_commercial: 'Needs commercial', needs_legal: 'Needs legal',
  failed: 'Failed',
};
const STATE_STYLES = {
  fully_answered: 'background:#e8f5e9;color:var(--success)',
  partially_answered: 'background:#fff4ce;color:#8a6d00',
  conflicting_evidence: 'background:#fff4ce;color:#8a6d00',
  failed: 'background:#fde7e9;color:#a4262c',
};
const PLACEHOLDER_STATES = ['no_evidence', 'needs_sme', 'needs_commercial', 'needs_legal'];

function normReqId(id) { return String(id ?? '').trim(); }
const respDomId = id => 'resp-' + encodeURIComponent(normReqId(id));
const blockDomId = id => 'req-block-' + encodeURIComponent(normReqId(id));

function formatTime(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
}

// ── An answer ────────────────────────────────────────────

function evidenceItemHtml(e) {
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
}

function stateBadgeHtml(state) {
  if (!(state in STATE_LABELS)) return '';
  return '<span class="badge" style="' + (STATE_STYLES[state] || 'background:#fde7e9;color:#a4262c') + '">' + esc(STATE_LABELS[state]) + '</span>';
}

/** The compliance, answer text, evidence, warnings, assumptions and risks of one answer. */
function answerBodyHtml(resp) {
  const meets = meetsLabel(resp.meets);
  const placeholder = PLACEHOLDER_STATES.includes(resp.state);
  const evidence = (resp.evidence || []).map(evidenceItemHtml).join('');
  const listed = (label, items, color) => (items || []).length
    ? '<div style="margin-top:4px"><strong style="font-size:11px">' + label + ':</strong> <span style="font-size:11px' +
      (color ? ';color:' + color : '') + '">' + items.map(esc).join('; ') + '</span></div>'
    : '';
  const body = placeholder
    ? '<div style="font-size:12px;line-height:1.6;margin-bottom:6px;white-space:pre-wrap;padding:8px 10px;border:1px dashed #a4262c;border-radius:6px;background:#fff8f8">' + esc(resp.answer_markdown || '') + '</div>'
    : '<div style="font-size:12px;line-height:1.6;margin-bottom:6px;white-space:pre-wrap">' + esc(resp.answer_markdown || '') + '</div>';
  const missing = resp.missing && !placeholder
    ? '<div style="margin-top:4px;font-size:11px;color:#8a6d00"><strong>Missing:</strong> ' + esc(resp.missing) + '</div>' : '';
  return '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">' +
      '<span style="font-weight:700;font-size:13px' + (meets ? '' : ';color:var(--text-muted)') + '" class="' + (MEETS_CLASSES[meets] || '') + '">' + esc(meets || 'Not assessed') + '</span>' +
      '<span style="font-size:11px;color:var(--text-muted)">Compliance</span>' + stateBadgeHtml(resp.state) + '</div>' +
    body + missing +
    (evidence ? '<div style="margin-top:6px"><strong style="font-size:11px">Evidence:</strong><ul style="list-style:none;margin:4px 0 0 0;padding:0">' + evidence + '</ul></div>' : '') +
    (resp.kb_evidence_found === false ? '<div style="margin-top:6px;font-size:11px;color:#8a6d00">&#9888; No internal knowledge-base evidence was found for this requirement.</div>' : '') +
    listed('Assumptions', resp.assumptions) + listed('Risks', resp.risks, 'var(--error)');
}

// ── A project's requirements ─────────────────────────────

function statusBadgeHtml(status) {
  const key = status in STATUS_LABELS ? status : 'extracted';
  return '<span class="badge" style="' + STATUS_STYLES[key] + '">' + esc(STATUS_LABELS[key]) + '</span>';
}

/** Who wrote the current answer, and when: "Version 3 · model · 22/10/2026 12:00". */
function answerMetaHtml(answer) {
  if (!answer) return '';
  const who = answer.source === 'model' ? 'drafted by the model' : 'written by ' + (answer.author_name || answer.author || 'someone');
  const restored = answer.restored_from ? ' (restored from version ' + Number(answer.restored_from) + ')' : '';
  return '<div style="font-size:10px;color:var(--text-muted);margin-bottom:6px">Version ' + Number(answer.version) + ' &middot; ' +
    esc(who) + restored + (answer.created_at ? ' &middot; ' + esc(formatTime(answer.created_at)) : '') + '</div>';
}

/** The action buttons under an answer. They carry data-action / data-rid; app code handles the clicks. */
function requirementActionsHtml(req, running) {
  const rid = esc(req.rid);
  const button = (action, label, extra) =>
    '<button class="btn btn-secondary btn-sm" data-action="' + action + '" data-rid="' + rid + '"' + (extra || '') + '>' + label + '</button>';
  const parts = [button('edit-answer', req.answer ? 'Edit answer' : 'Write answer')];
  // Only an answer the sources support can be approved; a placeholder or conflict needs a person to write it first
  const approvable = !req.state || ['fully_answered', 'partially_answered'].includes(req.state);
  if (req.answer && req.status !== 'approved' && approvable) parts.push(button('approve', 'Approve'));
  if (req.answer) parts.push(button('versions', 'Versions (' + Number(req.answer_version) + ')'));
  parts.push(button('history', 'History'));
  parts.push(button('redraft', req.answer ? 'Redraft' : 'Draft this', running ? ' disabled title="A draft is running"' : ''));
  return '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">' + parts.join('') + '</div>';
}

function requirementBodyHtml(req, running) {
  const answer = req.answer
    ? answerMetaHtml(req.answer) + answerBodyHtml(req.answer)
    : (req.state === 'failed'
      ? '<em style="color:#a4262c">Drafting failed for this requirement. Draft again to retry.</em>'
      : '<em style="color:var(--text-muted)">No answer yet.</em>');
  return answer + requirementActionsHtml(req, running) +
    '<div class="req-extra" data-for="' + esc(req.rid) + '"></div>';
}

/** One requirement of a project with its answer, status and actions. */
function projectRequirementHtml(req, running) {
  const category = req.category
    ? ' <span style="color:var(--text-muted);font-size:10px">[' + esc(req.category) + ']</span>' : '';
  const kind = req.type === 'question'
    ? '<span class="badge" style="margin-top:1px;background:#fff4ce;color:#8a6d00">question</span>' : '';
  const anchor = req.anchor
    ? '<span style="color:var(--text-muted);font-size:10px;white-space:nowrap" title="Where this is in the RFP">' + esc(req.anchor) + '</span>' : '';
  const assignee = req.assignee
    ? '<span style="color:var(--text-muted);font-size:10px;white-space:nowrap" title="Assigned to">&#128100; ' + esc(req.assignee_name || req.assignee) + '</span>' : '';
  return '<div class="req-block" data-rid="' + esc(req.rid) + '" data-rev="' + Number(req.rev) + '" id="' + esc(blockDomId(req.rid)) +
    '" style="border:1px solid var(--border);border-radius:8px;margin-bottom:10px;overflow:hidden">' +
    '<div style="padding:10px 12px;display:flex;gap:8px;align-items:flex-start;background:#fafafa;border-bottom:1px solid var(--border)">' +
      '<span style="font-weight:700;color:var(--primary);min-width:40px;font-size:12px">' + esc(req.rid) + '</span>' +
      kind + '<span class="badge ' + priorityClass(req.priority) + '" style="margin-top:1px">' + esc(req.priority) + '</span>' + category +
      '<span style="flex:1;font-size:12px">' + esc(req.text) + '</span>' + assignee + anchor + statusBadgeHtml(req.status) +
    '</div>' +
    '<div class="req-response" id="' + esc(respDomId(req.rid)) + '" style="padding:10px 12px;font-size:12px">' +
      requirementBodyHtml(req, running) + '</div></div>';
}

function countsHtml(counts, total) {
  const item = (label, n, color) => '<span style="color:' + color + '"><strong>' + Number(n) + '</strong> ' + label + '</span>';
  return '<div style="display:flex;gap:14px;font-size:12px;flex-wrap:wrap">' +
    item('no answer', counts.extracted || 0, 'var(--text-muted)') + item('drafted', counts.drafted || 0, '#0f6cbd') +
    item('needs input', counts.needs_input || 0, '#a4262c') +
    item('edited', counts.edited || 0, '#8a6d00') + item('approved', counts.approved || 0, 'var(--success)') +
    '<span style="color:var(--text-muted)">of ' + Number(total) + '</span></div>';
}

// ── Editing, versions and history ────────────────────────

function editFormHtml(req) {
  const answer = req.answer || {};
  const meets = ['Yes', 'Partial', 'No'].map(m =>
    '<option value="' + m + '"' + (answer.meets === m ? ' selected' : '') + '>' + m + '</option>').join('') +
    (answer.answer_markdown && !answer.meets ? '<option value="" selected>Not assessed</option>' : '');
  // A placeholder is not text to edit: start blank and say what is needed
  const blank = PLACEHOLDER_STATES.includes(answer.state);
  const needed = blank && answer.missing
    ? '<div style="font-size:11px;color:#a4262c;margin-bottom:4px">Needed: ' + esc(answer.missing) + '</div>' : '';
  return '<div class="edit-form" style="margin-top:8px;padding:10px;border:1px solid var(--border);border-radius:6px;background:#fff">' +
    '<label>Answer</label>' + needed + '<textarea class="edit-text" rows="6">' + (blank ? '' : esc(answer.answer_markdown || '')) + '</textarea>' +
    '<div style="display:flex;gap:8px;align-items:center;margin-top:6px"><label style="margin:0">Compliance</label>' +
    '<select class="edit-meets" style="width:auto">' + meets + '</select>' +
    '<span style="flex:1"></span>' +
    '<button class="btn btn-primary btn-sm" data-action="save-answer" data-rid="' + esc(req.rid) + '" data-rev="' + Number(req.rev) + '">Save as new version</button>' +
    '<button class="btn btn-secondary btn-sm" data-action="close-extra" data-rid="' + esc(req.rid) + '">Cancel</button></div>' +
    '<div style="font-size:10px;color:var(--text-muted);margin-top:4px">The earlier version is kept. An approved answer needs approving again.</div></div>';
}

function versionsHtml(rid, answers, currentVersion, rev) {
  if (!answers.length) return '<div style="font-size:11px;color:var(--text-muted)">No versions.</div>';
  return '<div style="margin-top:8px;border:1px solid var(--border);border-radius:6px;padding:8px;background:#fff">' +
    '<strong style="font-size:11px">Versions</strong>' +
    answers.map(a => {
      const current = a.version === currentVersion;
      const who = a.source === 'model' ? 'model' : (a.author_name || a.author || '');
      const restore = current ? '<span class="badge" style="background:#e8f5e9;color:var(--success)">current</span>'
        : '<button class="btn btn-secondary btn-sm" data-action="restore" data-rid="' + esc(rid) + '" data-version="' + Number(a.version) +
          '" data-rev="' + Number(rev) + '">Restore</button>';
      return '<div style="padding:6px 0;border-top:1px solid #f0f0f0;font-size:11px">' +
        '<div style="display:flex;gap:8px;align-items:center"><strong>v' + Number(a.version) + '</strong>' +
        '<span style="color:var(--text-muted)">' + esc(who) + ' &middot; ' + esc(formatTime(a.created_at)) + '</span>' +
        '<span style="flex:1"></span>' + restore + '</div>' +
        '<div style="margin-top:3px;white-space:pre-wrap;color:#323130">' + esc(String(a.answer_markdown || '').substring(0, 400)) + '</div></div>';
    }).join('') +
    '<div style="margin-top:6px"><button class="btn btn-secondary btn-sm" data-action="close-extra" data-rid="' + esc(rid) + '">Close</button></div></div>';
}

/** One audit event as a sentence. Answer text is never in an event, so this shows who and what, not the content. */
function describeEvent(e) {
  const d = e.details || {};
  switch (e.action) {
    case 'project.created': return 'created the project with ' + Number(d.requirements || 0) + ' requirements';
    case 'project.updated': return 'changed the project (' + Object.keys(d).map(esc).join(', ') + ')';
    case 'requirement.updated': return 'changed ' + Object.keys(d.changes || {}).map(esc).join(', ');
    case 'answer.added': return (d.restored_from ? 'restored version ' + Number(d.restored_from) + ' as ' : 'added ') +
      'answer version ' + Number(d.version) + (d.source === 'model' ? ' (drafted by the model)' : '');
    case 'draft.started': return 'started a draft of ' + Number(d.requirements || 0) + ' requirements';
    case 'draft.finished': return 'draft ' + esc(d.outcome || '') + ': ' + Number(d.saved || 0) + ' saved' +
      (d.changed_meanwhile ? ', ' + Number(d.changed_meanwhile) + ' changed meanwhile' : '') +
      (d.failed ? ', ' + Number(d.failed) + ' failed' : '');
    default: return esc(e.action);
  }
}

function eventsHtml(events, rid) {
  if (!events.length) return '<div style="font-size:11px;color:var(--text-muted)">Nothing recorded yet.</div>';
  return '<div style="margin-top:8px;border:1px solid var(--border);border-radius:6px;padding:8px;background:#fff">' +
    '<strong style="font-size:11px">' + (rid ? 'History of ' + esc(rid) : 'Recent activity') + '</strong>' +
    events.map(e => '<div style="padding:4px 0;border-top:1px solid #f0f0f0;font-size:11px">' +
      '<span style="color:var(--text-muted)">' + esc(formatTime(e.at)) + '</span> &middot; <strong>' +
      esc(e.actor_name || e.actor || '') + '</strong> ' + describeEvent(e) +
      (e.rid && !rid ? ' <span style="color:var(--text-muted)">(' + esc(e.rid) + ')</span>' : '') + '</div>').join('') +
    (rid ? '<div style="margin-top:6px"><button class="btn btn-secondary btn-sm" data-action="close-extra" data-rid="' + esc(rid) + '">Close</button></div>' : '') +
    '</div>';
}

// ── The list of projects ─────────────────────────────────

function projectListHtml(projects) {
  if (!projects.length) {
    return '<div style="font-size:12px;color:var(--text-muted);padding:6px 0">No projects yet. Upload an RFP below to start one.</div>';
  }
  return projects.map(p => {
    const shared = p.groups && p.groups.length ? 'shared with ' + p.groups.length + ' group(s)' : 'private';
    return '<div class="kb-doc-item"><div style="flex:1"><strong>' + esc(p.name) + '</strong>' +
      '<div class="doc-meta">' + Number(p.requirement_count || 0) + ' requirements &middot; owner ' + esc(p.owner_name || p.owner) +
      ' &middot; ' + shared + ' &middot; ' + esc(formatTime(p.created_at)) + '</div></div>' +
      '<button class="btn btn-primary btn-sm" data-action="open-project" data-project="' + esc(p.id) + '">Open</button></div>';
  }).join('');
}

if (typeof module !== 'undefined') {
  module.exports = {
    meetsLabel, priorityClass, normReqId, respDomId, blockDomId, formatTime, evidenceItemHtml, answerBodyHtml,
    statusBadgeHtml, stateBadgeHtml, answerMetaHtml, requirementActionsHtml, projectRequirementHtml, countsHtml, editFormHtml,
    versionsHtml, describeEvent, eventsHtml, projectListHtml,
  };
}
