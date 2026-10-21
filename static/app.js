const token = new URLSearchParams(location.search).get('token') || '';
const $ = (id) => document.getElementById(id);
let state = { bots: [], groups: [], grants: [], key_status: {}, runs: [] };
let selectedBotId = null;
let selectedGroupId = new URLSearchParams(location.search).get('group');
let selectedFiles = new Set();
let drafts = new Map();
let events = null;
// Messages per group, loaded when a chat opens and then kept current by pushed events.
const conversations = new Map();
let workflowViewKey = '';

async function api(path, body) {
  const options = { headers: { 'X-Local-Token': token } };
  if (body !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (response.status === 401 && data.login_required) showLogin();
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status})`);
    error.data = data;
    throw error;
  }
  return data;
}

// With Supabase configured, the app waits behind this sign-in screen.
function showLogin() {
  stopEvents();
  conversations.clear();
  for (const id of ['bots-page', 'chats-page', 'account', 'import-card']) $(id).hidden = true;
  document.querySelector('.main-nav').hidden = true;
  $('login-page').hidden = false;
  $('login-form').hidden = false;
  $('login-email').focus();
}

function showAccount(session) {
  $('login-page').hidden = true;
  document.querySelector('.main-nav').hidden = false;
  $('account').hidden = session.mode !== 'supabase';
  $('account-email').textContent = session.email;
  if (session.mode === 'supabase') $('footer-note').textContent = 'Bots and chats are saved to your Supabase account. API keys and Desktop file permissions stay on this computer.';
}

let shownSyncError = '';
// Shown once per distinct problem, not on every update while it lasts.
function showSyncError() {
  if (state.sync_error && state.sync_error !== shownSyncError) notice(state.sync_error, true);
  shownSyncError = state.sync_error || '';
}

function renderSync(status) {
  const pill = $('sync-status');
  pill.hidden = !status || status.state === 'local';
  if (pill.hidden) return;
  const waiting = `${status.pending} change${status.pending === 1 ? '' : 's'}`;
  pill.textContent = status.state === 'offline' ? `Offline · ${waiting} waiting` : status.state === 'saving' ? `Saving ${waiting}…` : 'All changes saved';
  if (status.failed) pill.textContent += ` · ${status.failed} not saved`;
  pill.className = `sync-pill ${status.failed ? 'failed' : status.state}`;
  pill.title = status.state === 'offline' ? `Supabase is not responding. Changes are kept on this computer and sent when it answers. (${status.reason})` : '';
}

function notice(message, error = false) {
  const box = $('notice');
  box.textContent = message;
  box.classList.toggle('error', error);
  box.hidden = false;
  clearTimeout(notice.timer);
  notice.timer = setTimeout(() => { box.hidden = true; }, 5500);
}

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function page() { return location.pathname === '/chats' ? 'chats' : 'bots'; }

function navigate(destination, groupId = selectedGroupId, push = true) {
  const params = new URLSearchParams({ token });
  if (destination === 'chats' && groupId) params.set('group', groupId);
  if (push) history.pushState({}, '', `/${destination}?${params}`);
  $('bots-page').hidden = destination !== 'bots';
  $('chats-page').hidden = destination !== 'chats';
  $('nav-bots').classList.toggle('active', destination === 'bots');
  $('nav-chats').classList.toggle('active', destination === 'chats');
  if (destination === 'bots') { renderBotList(); renderBotEditor(); }
  else { renderGroupList(); renderChat(true); }
}

function findBot(id) { return state.bots.find((bot) => bot.id === id); }
// Each bot keeps one colour everywhere. Roster order keeps colours distinct; removed bots fall back to a hash.
function voice(id) {
  let index = state.bots.findIndex((bot) => bot.id === id);
  if (index < 0) {
    index = 2166136261;
    for (const char of String(id)) index = Math.imul(index ^ char.charCodeAt(0), 16777619);
    index = (index ^ (index >>> 15)) >>> 0;
  }
  return `voice-${index % 8}`;
}
function findGroup(id) { return state.groups.find((group) => group.id === id); }

function renderBotList() {
  const host = $('bot-list'); host.replaceChildren();
  $('bot-count').textContent = String(state.bots.length);
  if (!state.bots.length) host.append(node('div', 'empty-list', 'No bots yet.'));
  for (const bot of state.bots) {
    const button = node('button', `rail-item${bot.id === selectedBotId ? ' active' : ''}`);
    button.type = 'button';
    const avatar = node('span', `rail-avatar ${voice(bot.id)}`, bot.name.slice(0, 1).toUpperCase());
    const copy = node('span', 'rail-copy');
    copy.append(node('strong', null, bot.name), node('small', null, bot.role || 'Role not set'));
    button.append(avatar, copy);
    button.addEventListener('click', () => selectBot(bot.id));
    host.append(button);
  }
}

function formDraft() {
  return {
    name: $('bot-name').value,
    role: $('bot-role').value,
    assignment: $('bot-assignment').value,
    endpoint: $('bot-endpoint').value,
    model: $('bot-model').value,
    api_format: $('bot-format').value,
    auth_header: $('bot-auth-header').value,
    auth_prefix: $('bot-auth-prefix').value,
    headers_text: $('bot-headers').value,
    request_template: $('bot-template').value,
    response_path: $('bot-response-path').value,
  };
}

function botDraft(bot) {
  return {
    name: bot.name, role: bot.role, assignment: bot.assignment,
    endpoint: bot.endpoint, model: bot.model, api_format: bot.api_format,
    auth_header: bot.auth_header, auth_prefix: bot.auth_prefix,
    headers_text: JSON.stringify(bot.extra_headers, null, 2),
    request_template: bot.request_template, response_path: bot.response_path,
  };
}

function stashDraft() {
  if (selectedBotId && !$('bot-editor').hidden) drafts.set(selectedBotId, formDraft());
}

function selectBot(id) {
  if (id === selectedBotId) return;
  stashDraft();
  selectedBotId = id;
  renderBotList();
  renderBotEditor();
}

function renderBotEditor() {
  const bot = findBot(selectedBotId);
  $('bot-empty').hidden = !!bot;
  $('bot-editor').hidden = !bot;
  if (!bot) return;
  const draft = drafts.get(bot.id) || botDraft(bot);
  $('editor-title').textContent = draft.name || 'New bot';
  $('bot-name').value = draft.name;
  $('bot-role').value = draft.role;
  $('bot-assignment').value = draft.assignment;
  $('bot-endpoint').value = draft.endpoint;
  $('bot-model').value = draft.model;
  $('bot-format').value = draft.api_format;
  $('bot-auth-header').value = draft.auth_header;
  $('bot-auth-prefix').value = draft.auth_prefix;
  $('bot-headers').value = draft.headers_text;
  $('bot-template').value = draft.request_template;
  $('bot-response-path').value = draft.response_path;
  $('bot-key').value = '';
  $('custom-fields').hidden = draft.api_format !== 'custom';
  const hasKey = !!state.key_status[bot.id];
  const vault = vaultState();
  const saved = vault.saved[bot.id];
  $('bot-key-status').textContent = hasKey ? (saved ? 'Saved key' : 'Key set for session') : saved ? 'Saved key · locked' : 'No key';
  $('bot-key-status').classList.toggle('ready', hasKey);
  $('key-saved-line').textContent = saved
    ? `Saved key ends in …${saved.hint || '····'}, saved ${since(saved.saved_at)}.${vault.unlocked ? '' : ' Unlock saved keys to use it.'}`
    : hasKey ? 'Set for this session only. It is cleared when the app stops.' : 'Paste a key to use this bot.';
  const canRemember = vault.available && vault.configured && vault.unlocked;
  $('remember-key').disabled = !canRemember;
  $('remember-key').checked = canRemember;
  $('remember-key-label').title = canRemember ? '' : !vault.available ? 'Install the cryptography package to remember keys.' : !vault.configured ? 'Set up a passphrase for saved keys first.' : 'Unlock saved keys first.';
  $('remove-key').hidden = !(saved || hasKey);
  $('save-key').textContent = hasKey || saved ? 'Replace key' : 'Set key';
  renderBotGroups();
  renderVault();
}

function switchRow(title, subtitle, checked, onToggle) {
  const row = node('div', 'toggle-row');
  const copy = node('div', 'toggle-copy');
  copy.append(node('strong', null, title));
  if (subtitle) copy.append(node('small', null, subtitle));
  const toggle = node('button', 'switch');
  toggle.type = 'button'; toggle.setAttribute('role', 'switch');
  toggle.setAttribute('aria-label', `${title} ${checked ? 'on' : 'off'}`);
  toggle.setAttribute('aria-checked', String(checked));
  toggle.addEventListener('click', onToggle);
  row.append(copy, toggle);
  return row;
}

async function setMembership(groupId, botId, enabled) {
  try {
    stashDraft();
    state = await api(`/api/groups/${groupId}/members`, { bot_id: botId, enabled });
    renderBotList(); renderBotGroups(); renderGroupList(); renderGroupMembers(); renderRunSettings(); updateGroupSummary();
  } catch (error) { notice(error.message, true); }
}

function renderBotGroups() {
  const host = $('bot-groups'); host.replaceChildren();
  if (!selectedBotId) return;
  if (!state.groups.length) {
    host.append(node('div', 'empty-list', 'No groups yet. Create one on the Group chats page.'));
    return;
  }
  for (const group of state.groups) {
    const joined = group.bot_ids.includes(selectedBotId);
    host.append(switchRow(group.name, `${group.bot_ids.length} bot${group.bot_ids.length === 1 ? '' : 's'}`, joined, () => setMembership(group.id, selectedBotId, !joined)));
  }
}

function renderGroupList() {
  const host = $('group-list'); host.replaceChildren();
  $('group-count').textContent = String(state.groups.length);
  if (!state.groups.length) host.append(node('div', 'empty-list', 'No group chats yet.'));
  for (const group of state.groups) {
    const button = node('button', `rail-item${group.id === selectedGroupId ? ' active' : ''}`);
    button.type = 'button';
    const avatar = node('span', 'rail-avatar', group.name.slice(0, 1).toUpperCase());
    const copy = node('span', 'rail-copy');
    const botCount = group.bot_ids.length;
    const messageCount = group.message_count;
    copy.append(node('strong', null, group.name), node('small', null, `${botCount} bot${botCount === 1 ? '' : 's'} · ${messageCount} message${messageCount === 1 ? '' : 's'}`));
    button.append(avatar, copy);
    button.addEventListener('click', () => selectGroup(group.id));
    host.append(button);
  }
}

function selectGroup(id) {
  selectedGroupId = id;
  navigate('chats', id);
}

function latestRun(groupId) { return state.runs.find((run) => run.group_id === groupId); }
function isActive(run) { return !!run && ['queued', 'running'].includes(run.status); }
// How many runs, in any group, are waiting ahead of this one for a free worker.
function runsAhead(run) { return state.runs.filter((other) => other.status === 'queued' && other.created_at < run.created_at).length; }

function renderRunSettings() {
  const group = findGroup(selectedGroupId);
  if (!group) return;
  $('group-mode').value = group.mode || 'conversation';
  $('workflow-settings').hidden = $('group-mode').value !== 'workflow';
  const master = $('group-master'); master.replaceChildren();
  if (!group.bot_ids.length) master.append(new Option('Add bots first', ''));
  for (const id of group.bot_ids) {
    const bot = findBot(id);
    if (bot) master.append(new Option(bot.name, id));
  }
  master.value = group.master_id || group.bot_ids[0] || '';
  $('group-quality').value = group.quality_criteria || '';
}

function runSettingsDraft() {
  return { mode: $('group-mode').value, master_id: $('group-master').value, quality_criteria: $('group-quality').value };
}

function renderWorkflowProgress(group, run) {
  const flow = run && run.mode === 'workflow' ? run.workflow : group.mode === 'workflow' ? group.last_workflow : null;
  const host = $('workflow-progress');
  const signature = JSON.stringify([group.id, flow]);
  if (signature === workflowViewKey) return;
  workflowViewKey = signature;
  host.replaceChildren(); host.hidden = !flow;
  if (!flow) return;
  const phases = { starting: 'Starting workflow', working: 'Worker preparing result', revising: 'Worker revising result', reviewing: 'Master reviewing quality', confirming: 'Worker confirming completion', handoff: 'Both approved · handing off', complete: 'All tasks approved', blocked: 'Waiting for your input', stopped: 'Workflow stopped' };
  host.append(node('strong', null, phases[flow.phase] || 'Task workflow'));
  const results = Array.isArray(flow.approved_results) ? flow.approved_results : [];
  const meter = node('div', 'step-meter');
  for (let index = 0; index < flow.task_total; index++) meter.append(node('span', index < results.length ? 'done' : index === results.length && flow.phase !== 'complete' ? 'current' : null));
  host.append(meter);
  host.append(node('p', 'section-note', `${results.length}/${flow.task_total} tasks approved by both bots${flow.phase !== 'complete' && flow.current_worker ? ` · Current worker: ${flow.current_worker}` : ''}${flow.revision ? ` · Revision ${flow.revision}` : ''}`));
  for (const result of results) {
    const details = document.createElement('details');
    details.append(node('summary', null, `${result.bot_name} · approved result`), node('pre', null, result.result));
    host.append(details);
  }
}

function updateGroupSummary() {
  const group = findGroup(selectedGroupId);
  if (!group) return;
  $('group-summary').textContent = `${group.bot_ids.length} bot${group.bot_ids.length === 1 ? '' : 's'} in this room · ${group.message_count} message${group.message_count === 1 ? '' : 's'}`;
  $('member-count').textContent = String(group.bot_ids.length);
  const run = latestRun(group.id);
  const status = $('run-status');
  status.className = 'pill';
  if (run && run.status === 'queued') {
    const ahead = runsAhead(run);
    status.textContent = ahead ? `Queued · ${ahead} ahead` : 'Queued · next';
    status.classList.add('running');
  }
  else if (run && run.status === 'running') {
    if (run.mode === 'workflow') status.textContent = `Workflow · ${run.workflow?.approved_results?.length || 0}/${Math.max(0, run.total - 1)} approved`;
    else if (run.continuous) status.textContent = `Ongoing · ${run.completed} turns`;
    else if (run.duration_seconds) {
      const seconds = Math.max(0, Math.ceil(run.ends_at - Date.now() / 1000));
      status.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')} left · ${run.completed} turns`;
    } else status.textContent = `${run.completed}/${run.total} replying`;
    status.classList.add('running');
  }
  else if (run && run.status === 'failed') { status.textContent = 'Needs attention'; status.classList.add('failed'); }
  else if (run && run.status === 'blocked') { status.textContent = 'Needs input'; status.classList.add('failed'); }
  else if (run && run.status === 'complete') { status.textContent = run.completion_reason === 'time_limit' ? `Time finished · ${run.completed} turns` : 'Complete'; status.classList.add('ready'); }
  else if (run && run.status === 'stopped') status.textContent = `Stopped · ${run.completed} turns`;
  else if (run && run.status === 'interrupted') { status.textContent = 'Interrupted'; status.classList.add('failed'); }
  else status.textContent = 'Ready';
  const problem = run && ['failed', 'blocked', 'interrupted'].includes(run.status);
  $('run-error').hidden = !problem;
  $('run-error').textContent = problem ? run.error : '';
  const missingKeys = group.bot_ids.map(findBot).filter((bot) => bot && bot.auth_header && !state.key_status[bot.id]);
  $('send-message').disabled = isActive(run) || group.bot_ids.length < 2 || missingKeys.length > 0;
  const workflow = group.mode === 'workflow';
  $('conversation-hint').textContent = group.bot_ids.length < 2 ? 'Add at least two bots to start.' : missingKeys.length ? `Set API keys in Bot setup for: ${missingKeys.map((bot) => bot.name).join(', ')}.` : workflow ? 'Workers follow their assignments in order. The master and current worker must both approve before handoff.' : 'Bots take turns until you stop. Each turn builds on the conversation so far.';
  $('send-message').textContent = workflow ? 'Start workflow' : 'Start conversation';
  $('stop-conversation').textContent = workflow ? 'Stop workflow' : 'Stop conversation';
  $('stop-conversation').hidden = !isActive(run);
  $('stop-conversation').disabled = false;
  for (const id of ['group-mode', 'group-master', 'group-quality', 'save-run-settings']) $(id).disabled = isActive(run);
  renderWorkflowProgress(group, run);
}

function renderGroupMembers() {
  const host = $('group-members'); host.replaceChildren();
  const group = findGroup(selectedGroupId);
  if (!group) return;
  if (!state.bots.length) { host.append(node('div', 'empty-list', 'Create a bot on the Bot setup page.')); return; }
  const joined = group.bot_ids.map(findBot).filter(Boolean);
  const others = state.bots.filter((bot) => !group.bot_ids.includes(bot.id));
  for (const bot of [...joined, ...others]) {
    const enabled = group.bot_ids.includes(bot.id);
    const workflow = group.mode === 'workflow';
    const masterId = group.master_id || group.bot_ids[0];
    const sequence = workflow ? group.bot_ids.filter((id) => id !== masterId) : group.bot_ids;
    const label = workflow && enabled ? `${bot.name} · ${bot.id === masterId ? 'Master' : `Task ${sequence.indexOf(bot.id) + 1}`}` : bot.name;
    const row = switchRow(label, bot.assignment || bot.role || 'No assignment', enabled, () => setMembership(group.id, bot.id, !enabled));
    if (enabled && sequence.includes(bot.id) && sequence.length > 1) {
      const controls = node('span', 'order-actions');
      for (const [symbol, direction] of [['↑', -1], ['↓', 1]]) {
        const button = node('button', 'order-button', symbol);
        button.type = 'button'; button.title = direction < 0 ? 'Move bot earlier' : 'Move bot later';
        button.setAttribute('aria-label', `${button.title}: ${bot.name}`);
        const index = sequence.indexOf(bot.id);
        button.disabled = direction < 0 ? index === 0 : index === sequence.length - 1;
        button.addEventListener('click', async () => {
          const ordered = [...sequence]; [ordered[index], ordered[index + direction]] = [ordered[index + direction], ordered[index]];
          const remaining = [...ordered];
          const order = workflow ? group.bot_ids.map((id) => id === masterId ? id : remaining.shift()) : ordered;
          try { state = await api(`/api/groups/${group.id}/order`, { bot_ids: order }); renderGroupMembers(); renderGroupList(); }
          catch (error) { notice(error.message, true); }
        });
        controls.append(button);
      }
      row.insertBefore(controls, row.lastChild);
    }
    host.append(row);
  }
}

function renderMessages() {
  const group = findGroup(selectedGroupId);
  const host = $('messages');
  const nearBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 90;
  host.replaceChildren();
  if (!group) return;
  const chat = conversations.get(group.id);
  if (!chat) {
    host.append(node('div', 'conversation-empty', 'Loading messages…'));
    loadMessages(group.id).then(() => { if (selectedGroupId === group.id) renderMessages(); }, (error) => notice(error.message, true));
    return;
  }
  if (chat.hasMore) {
    const earlier = node('button', 'link-button load-earlier', 'Show earlier messages');
    earlier.type = 'button';
    earlier.addEventListener('click', () => loadEarlier(group.id));
    host.append(earlier);
  }
  if (!chat.messages.length) host.append(node('div', 'conversation-empty', 'This group is ready. Send a message to begin.'));
  for (const message of chat.messages) {
    const isUser = !message.bot_id;
    const item = node('article', `message${isUser ? ' user' : ` ${voice(message.bot_id)}`}`);
    item.append(node('div', 'message-avatar', isUser ? 'Y' : message.sender.slice(0, 1).toUpperCase()));
    const content = node('div', 'message-content');
    const head = node('div', 'message-head');
    head.append(node('strong', null, message.sender));
    if (message.created_at) {
      const time = new Date(message.created_at);
      if (!Number.isNaN(time.getTime())) head.append(node('span', null, time.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })));
    }
    const bubble = node('div', 'message-bubble', message.text);
    if (message.image) {
      const image = node('img', 'message-image');
      image.src = `/media/${encodeURIComponent(message.image)}?${new URLSearchParams({ token })}`;
      image.alt = message.text.slice(0, 300);
      image.loading = 'lazy';
      bubble.prepend(image);
    }
    content.append(head, bubble);
    if (message.files && message.files.length) content.append(node('div', 'attached-files', `Attached: ${message.files.map((path) => path.split('/').pop()).join(', ')}`));
    item.append(content); host.append(item);
  }
  const run = latestRun(group.id);
  if (run && run.status === 'queued') {
    const ahead = runsAhead(run);
    host.append(node('div', 'waiting', `Waiting for a free slot${ahead ? ` · ${ahead} run${ahead === 1 ? '' : 's'} ahead` : ''}…`));
  }
  else if (run && run.status === 'running') {
    const speaker = state.bots.find((bot) => bot.name === run.current_bot);
    host.append(node('div', `waiting${speaker ? ` ${voice(speaker.id)}` : ''}`, run.waiting_reason || (run.current_bot ? `${run.current_bot} is replying…` : 'Starting the conversation…')));
  }
  if (nearBottom || chat.messages.length < 3) host.scrollTop = host.scrollHeight;
}

function renderChat(full = false) {
  const group = findGroup(selectedGroupId);
  $('chat-empty').hidden = !!group;
  $('chat-content').hidden = !group;
  if (!group) return;
  if (full) $('group-name').value = group.name;
  if (full) renderRunSettings();
  updateGroupSummary();
  renderMessages();
  renderGroupMembers();
  renderGrants();
}

function renderGrants() {
  selectedFiles = new Set([...selectedFiles].filter((path) => state.grants.includes(path)));
  const host = $('grants'); host.replaceChildren();
  if (!state.grants.length) host.append(node('div', 'empty-list', 'No files allowed yet.'));
  for (const path of state.grants) {
    const row = node('div', 'grant-row');
    const checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.checked = selectedFiles.has(path);
    checkbox.setAttribute('aria-label', `Attach ${path} to this message`);
    checkbox.addEventListener('change', () => { if (checkbox.checked) selectedFiles.add(path); else selectedFiles.delete(path); updateAttachedCount(); });
    const info = node('div', 'grant-name');
    info.append(node('strong', null, path.split('/').pop()), node('small', null, path));
    const remove = node('button', 'remove-button', '×'); remove.type = 'button'; remove.title = 'Remove file permission';
    remove.setAttribute('aria-label', `Remove permission for ${path}`);
    remove.addEventListener('click', async () => {
      try { state = await api('/api/grants/remove', { path }); renderGrants(); notice('File permission removed.'); }
      catch (error) { notice(error.message, true); }
    });
    row.append(checkbox, info, remove); host.append(row);
  }
  updateAttachedCount();
}

function updateAttachedCount() {
  const count = selectedFiles.size;
  $('attached-count').textContent = count ? `${count} Desktop file${count === 1 ? '' : 's'} attached to this message` : 'No Desktop files attached';
}

async function loadMessages(groupId) {
  const page = await api(`/api/groups/${groupId}/messages`);
  conversations.set(groupId, { messages: page.messages, hasMore: page.has_more });
}

async function loadEarlier(groupId) {
  const chat = conversations.get(groupId);
  if (!chat?.messages.length) return;
  const host = $('messages');
  const fromBottom = host.scrollHeight - host.scrollTop;
  try {
    const page = await api(`/api/groups/${groupId}/messages?${new URLSearchParams({ before: chat.messages[0].id })}`);
    chat.messages = [...page.messages, ...chat.messages];
    chat.hasMore = page.has_more;
    renderMessages();
    host.scrollTop = host.scrollHeight - fromBottom;
  } catch (error) { notice(error.message, true); }
}

// Fetches only messages newer than the last one this page has.
async function loadNewMessages(groupId) {
  const chat = conversations.get(groupId);
  if (!chat) return;
  const last = chat.messages.at(-1);
  if (!last) { await loadMessages(groupId); return; }
  const page = await api(`/api/groups/${groupId}/messages?${new URLSearchParams({ after: last.id })}`);
  if (page.reset) conversations.set(groupId, { messages: page.messages, hasMore: page.has_more });
  else for (const message of page.messages) addMessage(groupId, message);
}

function addMessage(groupId, message) {
  const chat = conversations.get(groupId);
  if (chat && !chat.messages.some((item) => item.id === message.id)) chat.messages.push(message);
}

async function refreshState() {
  state = await api('/api/state');
  renderGroupList(); renderBotGroups(); renderChat(false);
  renderSync(state.sync);
  renderVault();
  showSyncError();
}

// Reloads everything this page shows, after connecting or reconnecting to the event stream.
async function resync() {
  try {
    await refreshState();
    await Promise.all([...conversations.keys()].filter((id) => findGroup(id)).map(loadNewMessages));
    renderChat(false);
  } catch (error) { notice(error.message, true); }
}

function applyRun({ run, sync_error: syncError }) {
  const index = state.runs.findIndex((item) => item.id === run.id);
  const previous = index >= 0 ? state.runs[index] : null;
  if (index >= 0) state.runs[index] = run; else state.runs.unshift(run);
  state.sync_error = syncError;
  if (isActive(previous) && ['failed', 'blocked'].includes(run.status)) notice(run.error, true);
  else showSyncError();
  renderGroupList();
  const selected = latestRun(selectedGroupId);
  if (page() === 'chats' && (run.group_id === selectedGroupId || selected?.status === 'queued')) { updateGroupSummary(); renderMessages(); renderGroupMembers(); }
}

function applyMessage({ group_id: groupId, message, message_count: count }) {
  const group = findGroup(groupId);
  if (group) group.message_count = count;
  addMessage(groupId, message);
  renderGroupList();
  if (groupId === selectedGroupId && page() === 'chats') { updateGroupSummary(); renderMessages(); }
}

function listen() {
  stopEvents();
  const source = new EventSource(`/api/events?${new URLSearchParams({ token })}`);
  events = source;
  const handle = (apply) => (event) => { try { apply(JSON.parse(event.data)); } catch (error) { notice(error.message, true); } };
  source.addEventListener('open', resync);
  source.addEventListener('message', handle(applyMessage));
  source.addEventListener('run', handle(applyRun));
  source.addEventListener('state', () => refreshState().catch((error) => notice(error.message, true)));
  source.addEventListener('resync', resync);
  source.addEventListener('sync', handle((status) => { state.sync = status; renderSync(status); }));
  source.addEventListener('error', async () => {
    // The browser retries dropped connections itself; a refused stream usually means the session ended.
    if (source.readyState !== EventSource.CLOSED || events !== source) return;
    try {
      const session = await api('/api/session');
      if (!session.signed_in) showLogin();
      else setTimeout(() => { if (events === source) listen(); }, 2000);
    } catch { setTimeout(() => { if (events === source) listen(); }, 2000); }
  });
}

function stopEvents() {
  if (events) events.close();
  events = null;
}

async function startApp() {
  conversations.clear();
  state = await api('/api/state');
  selectedBotId = state.bots[0]?.id || null;
  if (!findGroup(selectedGroupId)) selectedGroupId = state.groups[0]?.id || null;
  navigate(page(), selectedGroupId, false);
  renderSync(state.sync);
  renderVault();
  showSyncError();
  listen();
  const vault = vaultState();
  if (vault.configured && !vault.unlocked && Object.keys(vault.saved).length) openVault('unlock');
}

async function initialize() {
  try {
    const session = await api('/api/session');
    if (!session.signed_in) { showLogin(); return; }
    showAccount(session);
    await startApp();
  } catch (error) { notice(error.message, true); }
}

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('login-submit');
  button.disabled = true;
  $('login-error').hidden = true;
  try {
    const session = await api('/api/login', { email: $('login-email').value, password: $('login-password').value });
    $('login-password').value = '';
    if (session.can_import) {
      $('login-form').hidden = true;
      $('import-card').hidden = false;
      $('account-email').textContent = session.email;
      return;
    }
    showAccount(session);
    await startApp();
  } catch (error) {
    $('login-error').textContent = error.message;
    $('login-error').hidden = false;
  } finally { button.disabled = false; }
});
$('import-local').addEventListener('click', async () => {
  $('import-local').disabled = true;
  try {
    await api('/api/import-local', {});
    showAccount(await api('/api/session'));
    await startApp();
    notice('Local bots and chats imported into your account.');
  } catch (error) { notice(error.message, true); }
  finally { $('import-local').disabled = false; }
});
$('skip-import').addEventListener('click', async () => {
  try { showAccount(await api('/api/session')); await startApp(); }
  catch (error) { notice(error.message, true); }
});
$('sign-out').addEventListener('click', async () => {
  let waiting = 0;
  try { waiting = (await api('/api/logout', {})).unsynced || 0; }
  finally {
    state = { bots: [], groups: [], grants: [], key_status: {}, runs: [] };
    showLogin();
    if (waiting) notice(`${waiting} change${waiting === 1 ? '' : 's'} could not reach Supabase yet. They are saved on this computer and will be sent the next time you sign in.`, true);
  }
});

$('nav-bots').addEventListener('click', () => { if (page() === 'chats') navigate('bots'); });
$('nav-chats').addEventListener('click', () => { if (page() === 'bots') { stashDraft(); navigate('chats'); } });
window.addEventListener('popstate', () => {
  selectedGroupId = new URLSearchParams(location.search).get('group') || selectedGroupId;
  navigate(page(), selectedGroupId, false);
});

$('new-bot').addEventListener('click', async () => {
  try {
    stashDraft();
    state = await api('/api/bots', {});
    selectedBotId = state.bots.at(-1).id;
    renderBotList(); renderBotEditor();
    $('bot-name').focus();
  } catch (error) { notice(error.message, true); }
});
$('bot-name').addEventListener('input', () => { $('editor-title').textContent = $('bot-name').value || 'New bot'; });
$('bot-format').addEventListener('change', () => { $('custom-fields').hidden = $('bot-format').value !== 'custom'; });
$('save-bot').addEventListener('click', async () => {
  if (!selectedBotId) return;
  try {
    const draft = formDraft();
    let extra_headers;
    try { extra_headers = JSON.parse(draft.headers_text || '{}'); }
    catch { throw new Error('Extra headers must be valid JSON.'); }
    const config = { ...draft, extra_headers };
    delete config.headers_text;
    state = await api(`/api/bots/${selectedBotId}`, config);
    drafts.delete(selectedBotId);
    renderBotList(); renderBotEditor(); renderGroupList(); renderGroupMembers();
    notice('Bot settings saved.');
  } catch (error) { notice(error.message, true); }
});
$('save-key').addEventListener('click', async () => {
  if (!selectedBotId) return;
  try {
    stashDraft();
    const key = $('bot-key').value;
    const remember = !$('remember-key').disabled && $('remember-key').checked;
    state = await api(`/api/bots/${selectedBotId}/key`, { key, remember });
    $('bot-key').value = '';
    renderBotEditor();
    notice(!key ? 'API key cleared.' : remember ? 'API key saved. It is encrypted and unlocks with your passphrase.' : 'API key set for this session.');
  } catch (error) { if (error.data?.vault_locked) openVault('unlock', error.message); else notice(error.message, true); }
});
$('remove-key').addEventListener('click', async () => {
  const bot = findBot(selectedBotId);
  if (!bot || !confirm(`Remove the API key for ${bot.name}? A saved copy is deleted too.`)) return;
  try {
    state = await api(`/api/bots/${bot.id}/key/remove`, {});
    renderBotEditor(); renderChat(false);
    notice('API key removed.');
  } catch (error) { notice(error.message, true); }
});

function vaultState() {
  return state.vault || { available: false, configured: false, unlocked: false, remembered: false, saved: {}, min_length: 4 };
}

function since(iso) {
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
  if (Number.isNaN(days)) return 'earlier';
  return days < 1 ? 'today' : days === 1 ? 'yesterday' : `${days} days ago`;
}

function renderVault() {
  const vault = vaultState();
  const count = Object.keys(vault.saved).length;
  const keys = `${count} saved key${count === 1 ? '' : 's'}`;
  const actions = $('vault-actions');
  actions.replaceChildren();
  const add = (label, handler, kind = 'secondary') => {
    const button = node('button', `button ${kind} small`, label);
    button.type = 'button';
    button.addEventListener('click', handler);
    actions.append(button);
  };
  if (!vault.available) {
    $('vault-summary').textContent = 'Keys last until the app stops. To save them, install the cryptography package: python3 -m pip install cryptography';
  } else if (!vault.configured) {
    $('vault-summary').textContent = 'Keys last until the app stops. Set a passphrase to save them encrypted, then unlock once per session.';
    add('Set up passphrase', () => openVault('setup'), 'primary');
  } else if (!vault.unlocked) {
    $('vault-summary').textContent = `${keys} · locked. Unlock once to use them until you sign out.`;
    add('Unlock', () => openVault('unlock'), 'primary');
    add('Reset', resetVault, 'danger-text');
  } else {
    $('vault-summary').textContent = `${keys} · unlocked until you sign out or the app restarts${vault.remembered ? ' · remembered on this device' : ''}.`;
    add('Lock', () => vaultCall('lock', {}, 'Saved keys locked.'));
    add('Change passphrase', () => openVault('change'));
    add('New recovery code', () => vaultCall('recovery', {}));
    if (vault.remembered) add('Forget this device', () => vaultCall('forget-device', {}, 'This device no longer unlocks saved keys by itself.'));
    add('Reset', resetVault, 'danger-text');
  }
}

async function vaultCall(action, body, message = '') {
  const result = await api(`/api/vault/${action}`, body);
  state = result;
  renderVault(); renderBotEditor(); renderBotList(); renderChat(false);
  if (result.recovery_code) showRecoveryCode(result.recovery_code);
  else if (message) notice(message);
  return result;
}

function resetVault() {
  if (!confirm('Delete your passphrase setup and every saved API key? Bots and chats stay. You will paste keys again.')) return;
  vaultCall('reset', { confirm: true }, 'Saved keys deleted.').catch((error) => notice(error.message, true));
}

function field(label, id, type = 'password', autocomplete = 'off') {
  const wrap = node('label', 'field', label);
  const input = document.createElement('input');
  Object.assign(input, { id, type, autocomplete, required: true });
  wrap.append(input);
  return wrap;
}

// One dialog serves every step: set up, unlock, change passphrase, and showing a recovery code.
function openVault(mode, note = '') {
  const vault = vaultState();
  const dialog = $('vault-dialog');
  const form = $('vault-form');
  form.replaceChildren();
  const error = node('p', 'vault-error');
  error.hidden = true;
  const buttons = node('div', 'vault-actions-row');
  const submit = node('button', 'button primary', mode === 'setup' ? 'Save passphrase' : mode === 'unlock' ? 'Unlock' : 'Change passphrase');
  submit.type = 'submit';
  const cancel = node('button', 'button secondary', mode === 'unlock' ? 'Later' : 'Cancel');
  cancel.type = 'button';
  cancel.addEventListener('click', () => dialog.close());
  buttons.append(cancel, submit);
  const remember = node('label', 'check');
  const rememberBox = document.createElement('input');
  Object.assign(rememberBox, { type: 'checkbox', id: 'vault-remember' });
  remember.append(rememberBox, ' Remember on this device, so it unlocks by itself here');
  let useCode = false;

  if (mode === 'setup') {
    form.append(node('h2', null, 'Protect saved keys'),
      node('p', null, `Saved API keys are encrypted with a passphrase only you know. Use at least ${vault.min_length} characters; longer is much harder to guess. You will also get a one-time recovery code.`),
      field('Passphrase', 'vault-passphrase', 'password', 'new-password'), field('Type it again', 'vault-confirm', 'password', 'new-password'), remember);
  } else if (mode === 'unlock') {
    const secret = field('Passphrase', 'vault-passphrase', 'password', 'current-password');
    const toggle = node('button', 'link-button', 'Use recovery code instead');
    toggle.type = 'button';
    toggle.addEventListener('click', () => {
      useCode = !useCode;
      secret.firstChild.textContent = useCode ? 'Recovery code' : 'Passphrase';
      $('vault-passphrase').type = useCode ? 'text' : 'password';
      $('vault-passphrase').value = '';
      toggle.textContent = useCode ? 'Use passphrase instead' : 'Use recovery code instead';
    });
    form.append(node('h2', null, 'Unlock saved keys'), node('p', null, note || 'Enter your passphrase once. Saved keys stay unlocked until you sign out or the app restarts.'), secret, toggle, remember);
  } else {
    form.append(node('h2', null, 'Change passphrase'), node('p', null, note || 'Saved keys stay as they are; only the lock on them changes.'),
      field('New passphrase', 'vault-passphrase', 'password', 'new-password'), field('Type it again', 'vault-confirm', 'password', 'new-password'));
  }
  form.firstChild.id = 'vault-title';
  form.append(error, buttons);
  form.onsubmit = async (event) => {
    event.preventDefault();
    const secret = $('vault-passphrase').value;
    if ($('vault-confirm') && secret !== $('vault-confirm').value) { error.textContent = 'The two passphrases do not match.'; error.hidden = false; return; }
    submit.disabled = true;
    try {
      if (mode === 'setup') {
        dialog.close();
        await vaultCall('setup', { passphrase: secret, remember: rememberBox.checked });
      } else if (mode === 'unlock') {
        const result = await vaultCall('unlock', useCode ? { recovery_code: secret, remember: rememberBox.checked } : { passphrase: secret, remember: rememberBox.checked });
        dialog.close();
        if (result.used_recovery_code) openVault('change', 'Unlocked with your recovery code. Choose a new passphrase now.');
        else notice('Saved keys unlocked.');
      } else {
        await vaultCall('change', { passphrase: secret }, 'Passphrase changed.');
        dialog.close();
      }
    } catch (failure) {
      if (!dialog.open) dialog.showModal();
      error.textContent = failure.message;
      error.hidden = false;
    } finally { submit.disabled = false; }
  };
  if (!dialog.open) dialog.showModal();
  $('vault-passphrase').focus();
}

function showRecoveryCode(code) {
  const dialog = $('vault-dialog');
  const form = $('vault-form');
  form.replaceChildren();
  const copy = node('button', 'button secondary', 'Copy');
  copy.type = 'button';
  copy.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(code); copy.textContent = 'Copied'; }
    catch { notice('Select the code and copy it by hand.', true); }
  });
  const done = node('button', 'button primary', 'I saved it');
  done.type = 'button';
  done.addEventListener('click', () => dialog.close());
  const buttons = node('div', 'vault-actions-row');
  buttons.append(copy, done);
  form.append(node('h2', null, 'Your recovery code'),
    node('p', null, 'If you forget your passphrase, this code unlocks your saved keys. It is shown only now; store it somewhere safe, like a password manager. Any earlier code stops working.'),
    node('div', 'recovery-code', code), buttons);
  form.onsubmit = (event) => event.preventDefault();
  if (!dialog.open) dialog.showModal();
}
$('delete-bot').addEventListener('click', async () => {
  const bot = findBot(selectedBotId);
  if (!bot || !confirm(`Remove ${bot.name} from all groups? Existing chat messages will stay.`)) return;
  try {
    state = await api(`/api/bots/${bot.id}/delete`, {});
    drafts.delete(bot.id);
    selectedBotId = state.bots[0]?.id || null;
    renderBotList(); renderBotEditor(); renderGroupList(); renderChat(false);
    notice('Bot removed.');
  } catch (error) { notice(error.message, true); }
});

$('new-group').addEventListener('click', async () => {
  try {
    state = await api('/api/groups', { name: 'New group' });
    selectedGroupId = state.groups.at(-1).id;
    navigate('chats', selectedGroupId);
    $('group-name').focus(); $('group-name').select();
  } catch (error) { notice(error.message, true); }
});
$('save-group-name').addEventListener('click', async () => {
  if (!selectedGroupId) return;
  try {
    state = await api(`/api/groups/${selectedGroupId}/rename`, { name: $('group-name').value });
    renderGroupList(); renderChat(true); renderBotGroups(); notice('Group renamed.');
  } catch (error) { notice(error.message, true); }
});
$('group-name').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); $('save-group-name').click(); } });
$('send-message').addEventListener('click', async () => {
  const group = findGroup(selectedGroupId);
  if (!group) return;
  const text = $('chat-input').value;
  $('send-message').disabled = true;
  try {
    const settings = runSettingsDraft();
    state = await api(`/api/groups/${group.id}/settings`, settings);
    await api(`/api/groups/${group.id}/send`, { text, files: [...selectedFiles], mode: settings.mode, continuous: settings.mode !== 'workflow' });
    $('chat-input').value = '';
    selectedFiles.clear();
    // The new message and the run's progress arrive as pushed events.
    renderGrants(); updateAttachedCount();
  } catch (error) {
    $('send-message').disabled = false;
    if (error.data?.vault_locked) { openVault('unlock', error.message); return; }
    $('run-error').textContent = error.message; $('run-error').hidden = false; notice(error.message, true);
  }
});

async function saveRunSettings() {
  if (!selectedGroupId) return;
  try {
    state = await api(`/api/groups/${selectedGroupId}/settings`, runSettingsDraft());
    renderRunSettings(); renderGroupMembers(); updateGroupSummary();
    notice('Run settings saved.');
  } catch (error) { notice(error.message, true); }
}
$('save-run-settings').addEventListener('click', saveRunSettings);
$('group-mode').addEventListener('change', () => { $('workflow-settings').hidden = $('group-mode').value !== 'workflow'; saveRunSettings(); });

$('stop-conversation').addEventListener('click', async () => {
  if (!selectedGroupId) return;
  $('stop-conversation').disabled = true;
  try {
    state = await api(`/api/groups/${selectedGroupId}/stop`, {});
    renderGroupList(); renderChat(false);
    notice('Conversation stopped.');
  } catch (error) { $('stop-conversation').disabled = false; notice(error.message, true); }
});

$('allow-file').addEventListener('click', async () => {
  try {
    state = await api('/api/grants', { path: $('file-path').value.trim() });
    $('file-path').value = '';
    renderGrants(); notice('Desktop file allowed. Select it to attach to a message.');
  } catch (error) { notice(error.message, true); }
});
$('pick-file').addEventListener('click', async () => {
  const button = $('pick-file'); button.disabled = true;
  try {
    state = await api('/api/pick-file', {});
    renderGrants(); notice('Desktop file allowed. Select it to attach to a message.');
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});

initialize();
