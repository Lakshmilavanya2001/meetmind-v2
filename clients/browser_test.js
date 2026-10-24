// Browser end-to-end test of the demo page: two headless Chromium callers, each
// with a WAV file as its (fake) microphone, join through https://localhost:8443.
//
//   NODE_PATH=/path/to/node_modules node clients/browser_test.js <a.wav> <b.wav> <out_dir>
//
// a.wav / b.wav are the scripted microphone input (silence + speech), see
// clients/make_browser_mics.py. Mid-call, B switches what they hear to English
// from the settings dialog. Set UI_DIR=services/s2s/app/static to test local,
// undeployed page files (the rest - API, LiveKit, agent - stays live).
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const [aWav, bWav, outDir] = process.argv.slice(2);
const DEMO_URL = process.env.DEMO_URL || "https://localhost:8443";
const UI_DIR = process.env.UI_DIR;
const room = `browser-${Date.now()}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function caller(name, wav, speak, hear, voice, contextOptions = {}) {
  const browser = await chromium.launch({
    args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
           `--use-file-for-fake-audio-capture=${wav}%noloop`, "--autoplay-policy=no-user-gesture-required",
           // pages fulfilled by page.route() count as non-local, and Chrome's Local Network
           // Access checks would then block their connection to the local LiveKit server
           ...(UI_DIR ? ["--disable-features=LocalNetworkAccessChecks"] : [])],
  });
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true, permissions: ["microphone"], ...contextOptions });
  const page = await ctx.newPage();
  if (UI_DIR) {
    const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };
    await page.route(/\/(index\.html|app\.js|style\.css)?(\?.*)?$/, (route) => {
      const u = new URL(route.request().url());
      if (u.pathname.startsWith("/api/")) return route.continue();
      const file = path.join(UI_DIR, u.pathname === "/" ? "index.html" : u.pathname.slice(1));
      if (!fs.existsSync(file)) return route.continue();
      route.fulfill({ body: fs.readFileSync(file), contentType: types[path.extname(file)] });
    });
  }
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await page.goto(`${DEMO_URL}/?room=${room}`);
  await page.waitForFunction(() => document.querySelectorAll("#speak option").length > 0);
  await page.fill("#name", name);
  await page.selectOption("#speak", speak);
  await page.selectOption("#hear", hear);
  await page.check(`input[name="voice"][value="${voice}"]`, { force: true });
  await page.click("#mic-test");                                   // pre-join microphone check
  await sleep(1500);
  await page.screenshot({ path: path.join(outDir, `ui_join_${name}.png`) });
  await page.click("#join-btn");
  await page.waitForSelector("#call:not([hidden])", { timeout: 15000 });
  return { name, browser, page, errors };
}

async function snapshot(c) {
  return c.page.evaluate(() => {
    const text = (el) => (el && !el.hidden ? el.textContent.trim() : "");
    return {
      translator: text(document.querySelector("#translator")),
      people: [...document.querySelectorAll(".person")].map((p) => p.textContent.trim()),
      latency: text(document.querySelector("#latency")),
      toasts: [...document.querySelectorAll(".toast p")].map((p) => p.textContent),
      audio: [...document.querySelectorAll("audio[data-lk]")].map((a) => `${a.dataset.lk}:${a.volume}`),
      feed: [...document.querySelectorAll(".utt")].map((u) => ({
        mine: u.classList.contains("mine"),
        head: text(u.querySelector(".utt-head")),
        main: text(u.querySelector(".utt-main")),
        sub: text(u.querySelector(".utt-sub:not(.via-english)")),
        via: text(u.querySelector(".via-english")),
      })),
    };
  });
}

(async () => {
  const A = await caller("Asha", aWav, "hi", "hi", "female");
  const B = await caller("Bala", bWav, "ta", "ta", "male");
  console.log("both joined room", room);
  await sleep(12000);
  await A.page.screenshot({ path: path.join(outDir, "ui_A_during.png") });   // mid-call, while A's words are being voiced
  await sleep(21000);                                                           // B speaks
  // B opens settings and switches what they hear to English
  await B.page.click('button[commandfor="settings"][command="show-modal"]');
  await B.page.waitForSelector("#settings[open]");
  await B.page.screenshot({ path: path.join(outDir, "ui_B_settings.png") });
  await B.page.selectOption("#hear-live", "en");
  await B.page.click('#settings button[command="close"]');
  console.log("B switched hearing to English");
  await sleep(17000);                                                           // A speaks again
  for (const c of [A, B]) {
    const s = await snapshot(c);
    console.log(`\n===== ${c.name}'s screen`);
    console.log(JSON.stringify(s, null, 1));
    console.log("page errors:", c.errors.length ? c.errors : "none");
    await c.page.screenshot({ path: path.join(outDir, `ui_${c.name}.png`) });
  }
  // narrow phone viewport and dark mode on B's page
  await B.page.setViewportSize({ width: 390, height: 844 });
  await B.page.emulateMedia({ colorScheme: "dark" });
  await sleep(500);
  await B.page.screenshot({ path: path.join(outDir, "ui_B_mobile_dark.png") });
  await A.browser.close();
  await B.browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
