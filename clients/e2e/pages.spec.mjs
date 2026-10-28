import path from "node:path";
import { chromium, expect, test } from "@playwright/test";
test.setTimeout(180_000);
const BASE = process.env.BASE_URL || "https://10.79.254.169:8444";

// Every page loads under the proxy's Content-Security-Policy without a blocked script, style, font,
// worker or connection, including the call page (LiveKit client, noise-removal worklet).
test("meetmind-v2: every page works under the security headers", async () => {
  const browser = await chromium.launch({ args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
    `--use-file-for-fake-audio-capture=${path.resolve("silence.wav")}`, "--autoplay-policy=no-user-gesture-required"] });
  const tag = Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  try {
    const ctx = await browser.newContext({ ignoreHTTPSErrors: true, baseURL: BASE });
    const page = await ctx.newPage();
    const blocked = [];
    page.on("console", (m) => { if (/Content Security Policy|Refused to/i.test(m.text())) blocked.push(`${page.url()}: ${m.text()}`); });
    page.on("pageerror", (e) => blocked.push(`${page.url()}: ${e.message}`));
    const r = await page.request.post("/api/auth/signup", { headers: { origin: BASE },
      data: { name: "Asha Rao", email: `asha-${tag}@v2.test`, password: "correct-horse-1", organization: `Acme ${tag}` } });
    expect(r.ok()).toBeTruthy();
    const head = await page.request.get("/login");
    expect(head.headers()["content-security-policy"]).toContain("frame-ancestors 'none'");
    for (const p of ["/dashboard", "/assistant", "/bookings", "/reminders", "/settings", "/search"]) {
      await page.goto(p);
      await page.waitForLoadState("networkidle");
    }
    await page.goto("/dashboard");
    await page.locator("#instant-title").fill("CSP check");
    await page.getByRole("button", { name: "Start a meeting now" }).click();
    await expect(page).toHaveURL(/\/m\/[a-z]{3}-[a-z]{4}-[a-z]{3}$/);
    const code = page.url().split("/m/")[1];
    await page.locator("#noise").selectOption("enhanced").catch(() => {});          // the RNNoise worklet, when offered
    await page.getByRole("button", { name: /join/i }).first().click();
    await expect(page.locator("body")).toContainText(/Connected|You|Mute|Leave/i, { timeout: 60_000 });
    await page.waitForTimeout(3000);
    await page.goto(`/meetings/${code}`);
    await page.waitForLoadState("networkidle");
    expect(blocked, blocked.join("\n")).toEqual([]);
  } finally {
    await browser.close();
  }
});
