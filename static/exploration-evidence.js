/* Present immutable tool evidence with exact text continuity, readable coverage, and optional technical details. */

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function endPosition(item) {
  return item.text.endsWith('\n')
    ? { line: item.line + 1, column: 0 }
    : { line: item.line, column: item.column + [...item.text].length };
}

function readGroups(payload) {
  const groups = [];
  for (const item of payload.items) {
    const previous = groups.at(-1);
    const version = item.file_version ?? payload.file_version;
    const path = item.path ?? payload.scope;
    const end = endPosition(item);
    // Never bridge missing bytes, skipped lines, changed revisions, or different files.
    if (previous && typeof version === 'string' && previous.path === path && previous.version === version
        && Number.isSafeInteger(item.byte_start) && previous.byteEnd === item.byte_start
        && previous.end.line === item.line && previous.end.column === item.column) {
      previous.parts.push(item.text);
      previous.end = end;
      previous.byteEnd = item.byte_end;
    } else {
      groups.push({ path, version, start: { line: item.line, column: item.column }, end,
        byteStart: item.byte_start, byteEnd: item.byte_end, parts: [item.text] });
    }
  }
  return groups.map((group, index) => ({ ...group, text: group.parts.join(''),
    wholeEnd: group.parts.at(-1).endsWith('\n') || (payload.reached_eof === true && index === groups.length - 1) }));
}

function rangeText(group) {
  const last = group.end.column === 0 && group.end.line > group.start.line ? group.end.line - 1 : group.end.line;
  if (group.start.column === 0 && group.wholeEnd) {
    return group.start.line === last ? `Line ${last}` : `Lines ${group.start.line}–${last}`;
  }
  if (group.start.line === group.end.line) return `Line ${group.start.line}, columns ${group.start.column}–${group.end.column}`;
  return `Line ${group.start.line}, column ${group.start.column} → line ${group.end.line}, column ${group.end.column}`;
}

function omissionText(counts) {
  const entries = Object.entries(counts || {}).filter(([, count]) => count > 0);
  return entries.length ? entries.map(([reason, count]) => `${reason.replaceAll('_', ' ')}: ${count}`).join(' · ') : 'None reported';
}

export function evidenceView(result, toolName) {
  let payload;
  try { payload = JSON.parse(result.output); } catch { return null; }
  if (!payload || payload.contract_version !== '0.1' || !Array.isArray(payload.items)) return null;
  const kind = toolName?.replace('helios_project_', '');
  if (!['read_file', 'list_files', 'search_text'].includes(kind)) return null;
  if (kind === 'read_file' && !payload.items.every(item => item && typeof item.text === 'string'
      && Number.isSafeInteger(item.line) && item.line >= 1 && Number.isSafeInteger(item.column) && item.column >= 0)) return null;
  const view = { kind, scope: payload.scope === '.' ? 'Workspace' : payload.scope, payload,
    groups: [], requested: null, returned: null, sources: [], ranges: [] };
  if (kind === 'read_file') {
    view.groups = readGroups(payload);
    const requested = payload.requested_range;
    if (requested) view.requested = `Lines ${requested.start_line}–${requested.start_line + requested.line_count - 1}`;
    view.returned = view.groups.length > 1 ? `${view.groups.length} saved ranges`
      : view.groups.length ? rangeText(view.groups[0]) : 'No text returned';
    view.status = payload.reached_eof === true ? 'End of file reached'
      : payload.next_cursor ? 'More of the requested range is available'
      : payload.complete === true ? 'Requested range finished' : 'Partial read result';
  } else {
    const count = payload.items.length;
    view.returned = kind === 'list_files' ? `${count} ${count === 1 ? 'entry' : 'entries'}` : `${count} ${count === 1 ? 'match' : 'matches'}`;
    view.status = payload.next_cursor ? 'More results are available'
      : payload.complete === true ? (kind === 'list_files' ? 'Folder listing finished' : 'Search finished within this scope') : 'Partial result';
  }
  const sources = new Map();
  for (const reference of result.references || []) {
    if (reference.file_version) {
      const key = JSON.stringify([reference.path, reference.file_version]);
      if (!sources.has(key)) sources.set(key, { path: reference.path, version: reference.file_version });
    }
    if (reference.start_line == null) continue;
    const range = { path: reference.path, version: reference.file_version,
      start: { line: reference.start_line, column: reference.start_column },
      end: { line: reference.end_line, column: reference.end_column }, byteStart: reference.byte_start, byteEnd: reference.byte_end };
    const previous = view.ranges.at(-1);
    if (previous && typeof range.version === 'string' && previous.path === range.path && previous.version === range.version
        && Number.isSafeInteger(range.byteStart) && previous.byteEnd === range.byteStart
        && previous.end.line === range.start.line && previous.end.column === range.start.column) {
      previous.end = range.end;
      previous.byteEnd = range.byteEnd;
    } else view.ranges.push(range);
  }
  view.sources = [...sources.values()];
  return view;
}

function addFact(list, label, value) {
  const row = node('div');
  row.append(node('dt', label), node('dd', value));
  list.append(row);
}

function disclosure(label) {
  const details = node('details', null, 'evidence-details');
  const summary = node('summary');
  const icon = node('i');
  icon.setAttribute('data-lucide', 'chevron-down');
  icon.setAttribute('aria-hidden', 'true');
  summary.append(node('h4', label, 'evidence-disclosure-title'), icon);
  const body = node('div', null, 'evidence-details-body');
  details.append(summary, body);
  return { details, body };
}

function scrollRegion(label, className = 'evidence-result-scroll') {
  const region = node('div', null, `${className} evidence-scroll`);
  region.tabIndex = 0;
  region.setAttribute('role', 'region');
  region.setAttribute('aria-label', label);
  return region;
}

function textBlock(text, label, scrollable = true) {
  const pre = node('pre', null, scrollable ? 'evidence-scroll' : null);
  if (scrollable) pre.tabIndex = 0;
  pre.setAttribute('aria-label', label);
  pre.append(node('code', text));
  return pre;
}

export function renderExplorationEvidence(target, result, toolName) {
  target.replaceChildren();
  const view = evidenceView(result, toolName);
  if (view) {
    const overview = node('section', null, 'evidence-overview');
    overview.append(node('h3', view.scope), node('p', view.status, 'evidence-result-status'));
    const facts = node('dl', null, 'evidence-summary');
    if (view.requested) addFact(facts, 'Requested', view.requested);
    addFact(facts, 'Returned', view.returned);
    overview.append(facts);
    if ([view.payload.omissions, view.payload.omission_totals].some(counts => Object.values(counts || {}).some(count => count > 0))) {
      overview.append(node('p', 'Some entries were omitted from this result sequence. See Result details for coverage.', 'evidence-omission-notice'));
    }
    target.append(overview);
    if (view.kind === 'read_file') {
      const content = view.groups.length > 1 ? scrollRegion('Saved text ranges') : target;
      for (const group of view.groups) {
        const section = node('section', null, 'evidence-text');
        section.append(node('h4', view.groups.length === 1 ? 'Saved text' : `${group.path} · ${rangeText(group)}`), textBlock(group.text, `Saved text: ${group.path} · ${rangeText(group)}`, view.groups.length === 1));
        content.append(section);
      }
      if (content !== target) target.append(content);
    } else if (view.kind === 'list_files') {
      const list = node('ul', null, 'evidence-entries');
      for (const item of view.payload.items) {
        const entry = node('li');
        const icon = node('i');
        icon.setAttribute('data-lucide', item.type === 'directory' ? 'folder' : 'file');
        icon.setAttribute('aria-hidden', 'true');
        entry.append(icon, node('span', `${item.path}${item.type === 'directory' ? '/' : ''}`));
        list.append(entry);
      }
      if (list.childElementCount) {
        const content = scrollRegion('Folder listing');
        content.append(list);
        target.append(content);
      }
    } else {
      const content = scrollRegion('Search matches');
      for (const item of view.payload.items) {
        const section = node('section', null, 'evidence-text');
        section.append(node('h4', `${item.path} · line ${item.line}, match column ${item.column}`));
        section.append(node('p', `Snippet starts at column ${item.snippet_column}${item.snippet_is_partial ? ' · partial line' : ''}`, 'evidence-snippet-position'));
        section.append(textBlock(item.text, `Saved match: ${item.path} · line ${item.line}`, false));
        content.append(section);
      }
      if (content.childElementCount) target.append(content);
    }
    const { details, body } = disclosure('Result details');
    const metadata = scrollRegion('Result details', 'evidence-metadata-scroll');
    const coverage = node('dl', null, 'evidence-summary');
    addFact(coverage, 'Omissions on this page', omissionText(view.payload.omissions));
    addFact(coverage, 'Accumulated omissions', omissionText(view.payload.omission_totals));
    if (view.payload.next_position) {
      const next = view.payload.next_position;
      addFact(coverage, 'Next read position', `Line ${next.line}, column ${next.column}`);
    }
    metadata.append(coverage, node('p', 'This saved result covers only the returned paths and ranges. More available results are not loaded automatically.'));
    if (view.groups.length || view.kind === 'search_text') metadata.append(node('p', 'Lines are 1-based; Unicode columns are 0-based. End positions are exclusive.'));
    if (view.sources.length) {
      const sources = node('ul', null, 'evidence-sources');
      for (const source of view.sources) {
        const item = node('li', source.path);
        item.append(node('small', `File revision: ${source.version}`));
        sources.append(item);
      }
      metadata.append(sources);
    }
    if (view.ranges.length) {
      const positions = node('ul', null, 'evidence-positions');
      for (const range of view.ranges) {
        positions.append(node('li', `${range.path} · ${rangeText(range)}${range.byteStart == null ? '' : ` · Bytes ${range.byteStart}–${range.byteEnd} (end exclusive)`}`));
      }
      metadata.append(positions);
    }
    body.append(metadata);
    target.append(details);
  } else {
    target.append(node('p', 'A formatted preview is unavailable for this saved result. Open the original result below.'));
  }
  const raw = disclosure('Original result (JSON)');
  raw.body.append(textBlock(result.output, 'Original saved tool result'));
  target.append(raw.details);
}
