import path from "node:path";
import { chromium, expect, test } from "@playwright/test";
test.setTimeout(180_000);
const BASE = process.env.BASE_URL || "https://10.79.254.169:8444";

// The voice and tone picker: Magpie's voices, what each language hears, a sample, joining with it,
// and changing it in the call (remembered for the meeting).
test("meetmind-v2: pick a voice and tone, hear a sample, join with it, change it in the call", async () => {
  const browser = await chromium.launch({ args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
    `--use-file-for-fake-audio-capture=${path.resolve("silence.wav")}`, "--autoplay-policy=no-user-gesture-required"] });
  const tag = Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  try {
    const page = await (await browser.newContext({ ignoreHTTPSErrors: true, baseURL: BASE })).newPage();
    const post = (url, data) => page.request.post(url, { headers: { origin: BASE }, data });
    expect((await post("/api/auth/signup", { name: "Asha Rao", email: `asha-${tag}@v2.test`, password: "correct-horse-1", organization: `Acme ${tag}` })).ok()).toBeTruthy();
    const code = (await (await post("/api/meetings", { title: "Voice check" })).json()).meeting.joinCode;

    await page.goto(`/m/${code}`);
    await expect(page.locator("#voice option")).not.toHaveCount(2, { timeout: 20_000 });      // more than the fallback pair
    expect(await page.locator("#voice option").count()).toBeGreaterThan(5);
    await page.locator("#voice").selectOption("Jason");
    await page.locator("#tone").selectOption("Jason.Happy");
    await expect(page.locator("#voice-heard")).toContainText("English Jason · Happy");
    await expect(page.locator("#voice-heard")).toContainText("Hindi Jason");

    const [sample] = await Promise.all([page.waitForResponse("**/api/voices/preview"), page.locator("#voice-preview").click()]);
    expect(sample.status()).toBe(200);
    expect(sample.headers()["content-type"]).toContain("audio/wav");
    expect(Number(sample.headers()["content-length"])).toBeGreaterThan(20_000);   // a few seconds of 22 kHz audio
    expect(sample.headers()["x-voice"]).toBe("Jason.Happy");

    const [joined] = await Promise.all([page.waitForResponse(`**/api/meetings/${code}/join`), page.locator("#join-btn").click()]);
    expect((await joined.request().postDataJSON()).voice).toBe("Jason.Happy");
    await page.locator('button[commandfor="settings"][command="show-modal"]').click();
    await expect(page.locator("#voice-live")).toHaveValue("Jason");
    await expect(page.locator("#tone-live")).toHaveValue("Jason.Happy");
    const [saved] = await Promise.all([page.waitForResponse(`**/api/meetings/${code}/preferences`), page.locator("#tone-live").selectOption("Jason.Calm")]);
    expect((await saved.json()).myPreferences.voice).toBe("Jason.Calm");
  } finally {
    await browser.close();
  }
});
