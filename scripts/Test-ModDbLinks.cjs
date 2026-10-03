/* Development experiment only. Dependencies and isolated browser profiles live
 * under ignored build/moddb-link-probe. Never exports cookies or signed URLs.
 * A person completes any challenge; archive probes read at most 1024 bytes.
 */
const fs = require('node:fs');
const path = require('node:path');
const https = require('node:https');
const { createRequire } = require('node:module');
const { parseArgs } = require('node:util');

const root = path.resolve(__dirname, '..');
const work = path.join(root, 'build', 'moddb-link-probe');
const deps = createRequire(path.join(work, 'package.json'));
const { values } = parseArgs({ options: {
  mode: { type: 'string', default: 'plain' },
  browser: { type: 'string', default: 'C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe' },
  timeout: { type: 'string', default: '180' },
  ids: { type: 'string', default: '306772,300660,246523' },
} });
if (!['plain', 'stealth'].includes(values.mode)) throw new Error('Invalid mode');
const ids = values.ids.split(',');
if (!ids.every(id => /^\d+$/.test(id))) throw new Error('Invalid addon IDs');
const timeout = Number(values.timeout) * 1000;
if (!(timeout > 0 && timeout <= 600000)) throw new Error('Invalid timeout');
const run = path.join(work, `${new Date().toISOString().replace(/[:.]/g, '-')}-${values.mode}`);
fs.mkdirSync(run, { recursive: true });
const report = { mode: values.mode, results: [] };
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const isCdn = value => {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password &&
      (!url.port || url.port === '443') && url.hostname.endsWith('.dl.dbolical.com');
  } catch { return false; }
};
const safeUrl = value => {
  try { const url = new URL(value); return url.origin + url.pathname; }
  catch { return '(invalid URL)'; }
};

function probeArchive(url, redirects = 0) {
  return new Promise(resolve => {
    // No browser cookies, User-Agent spoofing, or clearance headers.
    const request = https.get(url, { headers: { Range: 'bytes=0-1023' } }, response => {
      if (response.statusCode >= 300 && response.statusCode < 400 && response.headers.location) {
        const next = new URL(response.headers.location, url).href;
        response.destroy();
        if (redirects < 3 && isCdn(next)) resolve(probeArchive(next, redirects + 1));
        else resolve({ accepted: false, status: response.statusCode, reason: 'Unexpected redirect' });
        return;
      }
      let size = 0;
      const chunks = [];
      const done = () => {
        const sample = Buffer.concat(chunks);
        const contentType = response.headers['content-type'] || '';
        const html = /text\/html/i.test(contentType) || /^\s*<(?:!doctype|html)/i.test(sample.toString('utf8'));
        resolve({ status: response.statusCode, content_type: contentType,
          content_range: response.headers['content-range'] || null,
          bytes_sampled: size, signature_hex: sample.subarray(0, 8).toString('hex'),
          accepted: [200, 206].includes(response.statusCode) && size > 0 && !html });
      };
      response.on('data', chunk => {
        const part = chunk.subarray(0, 1024 - size);
        chunks.push(part); size += part.length;
        if (size >= 1024) { done(); response.destroy(); }
      });
      response.on('end', done);
      response.on('error', () => resolve({ accepted: false, reason: 'Archive response interrupted' }));
    });
    request.setTimeout(20000, () => request.destroy());
    request.on('error', error => resolve({ accepted: false, error_code: error.code || error.name }));
  });
}

(async () => {
  let browser;
  try {
    let puppeteer = deps('puppeteer-core');
    if (values.mode === 'stealth') {
      puppeteer = deps('puppeteer-extra').addExtra(puppeteer);
      puppeteer.use(deps('puppeteer-extra-plugin-stealth')());
    }
    browser = await puppeteer.launch({ executablePath: values.browser,
      headless: false, userDataDir: path.join(run, 'profile'), defaultViewport: null,
      args: ['--no-first-run', '--no-default-browser-check'] });
    report.browser_version = await browser.version();
    const page = (await browser.pages())[0] || await browser.newPage();
    const session = await page.createCDPSession();
    await session.send('Browser.setDownloadBehavior', { behavior: 'deny', eventsEnabled: true });
    await session.send('Network.enable');
    let active;
    let signedUrl;
    const capture = value => {
      if (active && !signedUrl && isCdn(value)) {
        signedUrl = value;
        active.archive_url = safeUrl(value);
        console.log('Captured archive redirect for addon ' + active.id);
      }
    };
    // Abort the archive request before the browser transfers the large file.
    await page.setRequestInterception(true);
    page.on('request', request => {
      if (isCdn(request.url())) { capture(request.url()); void request.abort().catch(() => {}); }
      else void request.continue().catch(() => {});
    });
    session.on('Network.requestWillBeSent', event => {
      capture(event.request.url);
      const headers = event.redirectResponse?.headers || {};
      const location = Object.entries(headers).find(([key]) => key.toLowerCase() === 'location')?.[1];
      if (location) {
        try { capture(new URL(location, event.redirectResponse.url).href); } catch { /* ignore malformed redirect */ }
      }
    });
    session.on('Browser.downloadWillBegin', event => capture(event.url));
    session.on('Network.responseReceived', event => {
      if (!active || event.type !== 'Document') return;
      const response = event.response;
      if (new URL(response.url).hostname !== 'www.moddb.com') return;
      const headers = Object.fromEntries(Object.entries(response.headers).map(([k, v]) => [k.toLowerCase(), v]));
      active.documents.push({ url: safeUrl(response.url), status: response.status,
        challenge: headers['cf-mitigated'] === 'challenge', ray: headers['cf-ray'] || null });
    });
    console.log(`Opened ${values.mode} browser. Complete verification there if prompted; do not manually navigate between addons.`);
    for (const id of ids) {
      signedUrl = undefined;
      active = { id, documents: [], challenge_observed: false, resolved: false };
      report.results.push(active);
      console.log('Resolving addon ' + id);
      const started = Date.now();
      void page.goto(`https://www.moddb.com/addons/start/${id}`, { waitUntil: 'domcontentloaded', timeout })
        .catch(() => {}); // Downloads and challenges can interrupt navigation.
      while (!signedUrl && Date.now() - started < timeout) {
        try {
          const state = await page.evaluate(() => ({ title: document.title,
            url: location.origin + location.pathname,
            challenge: !!window._cf_chl_opt || document.title === 'Just a moment...' }));
          active.last_page = state;
          if (state.challenge && !active.challenge_observed) {
            active.challenge_observed = true;
            console.log(`Addon ${id}: verification required in the browser.`);
          }
        } catch { /* Navigation destroys execution contexts; retry on next poll. */ }
        await sleep(500);
      }
      active.elapsed_seconds = Math.round((Date.now() - started) / 1000);
      if (!signedUrl) {
        active.reason = 'No archive redirect before timeout';
        console.log(active.reason + '; stopping this session.');
        break;
      }
      active.resolved = true;
      active.native_download = await probeArchive(signedUrl);
      console.log('Native archive sample: ' + JSON.stringify(active.native_download));
      signedUrl = undefined;
      const completed = active;
      active = undefined;
      if (!completed.native_download.accepted) break;
      await sleep(3000);
    }
    report.all_succeeded = report.results.length === ids.length && report.results.every(item => item.native_download?.accepted);
    process.exitCode = report.all_succeeded ? 0 : 2;
  } catch (error) {
    // Error messages may contain signed URLs. Record only the error class.
    report.error_type = error.name;
    console.log('Probe failed: ' + error.name);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close().catch(() => {});
    fs.writeFileSync(path.join(run, 'report.json'), JSON.stringify(report, null, 2) + '\n');
    console.log('Report: ' + path.join(run, 'report.json'));
  }
})();
