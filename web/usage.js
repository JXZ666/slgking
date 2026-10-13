// Anonymous product usage only. No account, search text or game identity.
import { WEB_VERSION } from "./version.js";
let acknowledged = false, launched = false;
const features = new Set(['catalogue', 'search', 'detail', 'library', 'forum', 'shop', 'tools']);
const randomId = () => globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
function deviceId() {
  try {
    let id = localStorage.getItem('slg_web_usage_id');
    if (!/^[A-Za-z0-9._:-]{1,100}$/.test(id || '')) {
      id = randomId(); localStorage.setItem('slg_web_usage_id', id);
    }
    return id;
  } catch { return null; }
}
function send(event, feature, eventId) {
  if (!acknowledged || document.hidden) return;
  const device = deviceId();
  const data = { platform: 'web', client_version: WEB_VERSION };
  if (feature) data.feature = feature;
  const body = { app: 'slgking', v: '2', event, event_id: eventId || randomId(), ts: new Date().toISOString(), data };
  if (device) body.device = device;
  void fetch('/report', { method: 'POST', credentials: 'omit', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), keepalive: true }).catch(() => {});
}
function activate() {
  if (!acknowledged || document.hidden) return;
  const device = deviceId();
  const day = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
  // Stable daily event identity prevents multi-tab retries counting twice.
  send('active', null, device ? `web-active:${device}:${day.replace(/[^0-9]/g, '')}` : randomId());
  if (!launched) { launched = true; send('launch'); }
}
export function allowUsage() { acknowledged = true; activate(); }
export function reportFeature(feature) { if (features.has(feature)) send('feature', feature); }
document.addEventListener('visibilitychange', activate);
