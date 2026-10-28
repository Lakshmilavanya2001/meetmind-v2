import path from "node:path";
import { chromium, expect, test } from "@playwright/test";
test.setTimeout(180_000);
const BASE = process.env.BASE_URL || "https://10.79.254.169:8444";

// Voice to voice on the assistant page: a recorded question plays into the microphone, listening stops
// after the pause, Parakeet's text appears as the message, the agents answer, and Magpie reads it out.
test("meetmind-v2: talk to the assistant and hear its reply", async () => {
  const browser = await chromium.launch({ args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
    `--use-file-for-fake-audio-capture=${path.resolve("speech_then_silence.wav")}`, "--autoplay-policy=no-user-gesture-required"] });
  const tag = Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  try {
    const page = await (await browser.newContext({ ignoreHTTPSErrors: true, baseURL: BASE })).newPage();
    expect((await page.request.post("/api/auth/signup", { headers: { origin: BASE },
      data: { name: "Asha Rao", email: `asha-${tag}@v2.test`, password: "correct-horse-1", organization: `Acme ${tag}` } })).ok()).toBeTruthy();
    await page.goto("/assistant");
    const heard = page.waitForResponse("**/api/assistant/transcribe**", { timeout: 60_000 });
    const spoken = page.waitForResponse("**/api/assistant/speak", { timeout: 120_000 });
    await page.getByRole("button", { name: "Speak to the assistant" }).click();
    await expect(page.locator("#voice-status")).toHaveText(/Listening/);
    const t = await (await heard).json();
    expect(t.text.toLowerCase()).toContain("launch review");
    await expect(page.locator(".msg.user").last()).toContainText(/launch review/i);
    await expect(page.locator(".msg.assistant").last()).toBeVisible({ timeout: 60_000 });
    const s = await spoken;
    expect(s.status()).toBe(200);
    expect(s.headers()["content-type"]).toContain("audio/wav");
    await expect(page.locator("#voice-status")).toHaveText(/Speaking|Tap the mic/, { timeout: 30_000 });
  } finally {
    await browser.close();
  }
});
