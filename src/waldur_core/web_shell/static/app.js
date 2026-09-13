import { FitAddon, Terminal, init } from '../vendor/ghostty-web/dist/ghostty-web.js';

const statusEl = document.getElementById('status');
const environmentEl = document.getElementById('environment');
const container = document.getElementById('terminal');

const CLOSE_REASONS = {
  1001: 'server shutting down',
  1006: 'connection lost',
  4000: 'shell exited',
  4401: 'link expired or already used; open a new one from the Waldur user menu',
  4408: 'idle timeout',
  4409: 'you already have a shell open',
};

function setStatus(text, kind = '') {
  statusEl.textContent = text;
  statusEl.dataset.kind = kind;
}

// "Full Name (username) · email", leaving out whatever the account lacks.
function describeUser(user) {
  const name = user.full_name ? `${user.full_name} (${user.username})` : user.username;
  return user.email ? `${name} · ${user.email}` : name;
}

// Which deployment this shell is attached to. The server sends it only after
// the ticket is accepted. Values are appended as text, never parsed as HTML.
function showEnvironment(env) {
  if (!env) return;
  const host = env.ip ? `${env.host} (${env.ip})` : env.host;
  const items = [
    ['Site', env.site_name],
    ['Portal', env.portal],
    ['Host', host],
    ['Database', env.database],
  ].filter(([, value]) => value);
  environmentEl.replaceChildren(
    ...items.map(([label, value]) => {
      const item = document.createElement('span');
      item.className = 'environment-item';
      const labelEl = document.createElement('span');
      labelEl.className = 'environment-label';
      labelEl.textContent = label;
      item.append(labelEl, value);
      return item;
    }),
  );
  const site = env.site_name || 'Waldur';
  document.title = env.portal ? `${site} (${env.portal}) · web shell` : `${site} · web shell`;
}

// The ticket travels in the URL fragment, which browsers never send to a
// server, so it cannot end up in an access log. Drop it from the address bar
// at once so it is not left in history either.
function takeTicketFromFragment() {
  const ticket = new URLSearchParams(location.hash.slice(1)).get('t');
  if (location.hash) {
    history.replaceState(null, '', location.pathname + location.search);
  }
  return ticket;
}

function socketUrl() {
  const url = new URL('ws', location.href);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.search = '';
  url.hash = '';
  return url;
}

async function openShell(ticket) {
  setStatus('Loading terminal…');
  await init();

  const term = new Terminal({
    fontSize: 14,
    cursorBlink: true,
    scrollback: 10000,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
  });
  const fit = new FitAddon();
  term.loadAddon(fit);
  term.open(container);
  fit.fit();
  fit.observeResize();

  const ws = new WebSocket(socketUrl());
  ws.binaryType = 'arraybuffer';
  const encoder = new TextEncoder();
  const send = (data) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(data);
  };

  ws.addEventListener('open', () => {
    setStatus('Authenticating…');
    ws.send(JSON.stringify({ type: 'auth', ticket, cols: term.cols, rows: term.rows }));
  });
  ws.addEventListener('message', (event) => {
    if (typeof event.data === 'string') {
      const message = JSON.parse(event.data);
      if (message.type === 'ready') {
        showEnvironment(message.environment);
        setStatus(`Connected as ${describeUser(message.user)}`, 'ok');
        term.focus();
      }
      return;
    }
    term.write(new Uint8Array(event.data));
  });
  ws.addEventListener('close', (event) => {
    const reason = CLOSE_REASONS[event.code] || event.reason || `code ${event.code}`;
    setStatus(`Disconnected: ${reason}`, 'error');
    term.write('\r\n\x1b[2m[disconnected]\x1b[0m\r\n');
  });

  // Keystrokes go as binary frames; control messages as text frames.
  term.onData((data) => send(encoder.encode(data)));
  term.onResize(({ cols, rows }) => send(JSON.stringify({ type: 'resize', cols, rows })));
}

const ticket = takeTicketFromFragment();
if (ticket) {
  openShell(ticket).catch((error) => setStatus(error.message, 'error'));
} else {
  setStatus('Open the web shell from the user menu in Waldur.', 'error');
}
