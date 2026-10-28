import path from "node:path";
import { chromium, expect, test } from "@playwright/test";
test.setTimeout(180_000);
const BASE = process.env.BASE_URL || "https://10.79.254.169:8444";
const launch = () => chromium.launch({ args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", `--use-file-for-fake-audio-capture=${path.resolve(process.env.SILENCE_WAV || "silence.wav")}`, "--autoplay-policy=no-user-gesture-required"] });
const newPage = async (b) => (await b.newContext({ ignoreHTTPSErrors: true, baseURL: BASE })).newPage();

test("meetmind-v2: sign up, organisation, start and join a meeting, end for all", async () => {
  const [ba, bb] = await Promise.all([launch(), launch()]);
  const tag = Date.now().toString(36);
  try {
    const a = await newPage(ba);
    await a.goto("/");
    await expect(a).toHaveURL(/\/login$/);
    await a.getByRole("tab", { name: "Create account" }).click();
    await a.getByLabel("Your name").fill("Asha Rao");
    await a.locator("#su-email").fill(`asha-${tag}@v2.test`);
    await a.locator("#su-password").fill("correct-horse-1");
    await a.getByText("Create one").click();
    await a.getByLabel("Organisation name").fill(`Acme ${tag}`);
    await a.getByRole("button", { name: "Create account" }).click();
    await expect(a).toHaveURL(/\/dashboard$/);
    const code = (await a.locator("#admin-code").textContent()).trim();
    expect(code.length).toBeGreaterThan(5);

    const b = await newPage(bb);
    await b.goto("/signup");
    await b.getByLabel("Your name").fill("Ben Das");
    await b.locator("#su-email").fill(`ben-${tag}@v2.test`);
    await b.locator("#su-password").fill("correct-horse-1");
    await b.getByLabel("Join code").fill(code);
    await b.getByRole("button", { name: "Create account" }).click();
    await expect(b).toHaveURL(/\/dashboard$/);
    await expect(b.locator("#admin-card")).toBeHidden();                  // members don't see the join code

    await a.locator("#instant-title").fill("Launch sync");
    await a.getByRole("button", { name: "Start a meeting now" }).click();
    await expect(a).toHaveURL(/\/m\/[a-z]{3}-[a-z]{4}-[a-z]{3}$/);
    const link = a.url();
    await expect(a.locator(".join-intro h1")).toHaveText("Launch sync");
    await expect(a.locator("#name")).toHaveValue("Asha Rao");
    await expect(a.locator("#room-field")).toBeHidden();
    await a.locator("#join-btn").click();
    await expect(a.locator("#call")).toBeVisible({ timeout: 30_000 });
    await expect(a.locator("#end-all")).toBeVisible();
    await expect(a.locator("#room-name")).toHaveText("Launch sync");

    await b.goto(link);
    await b.locator("#join-btn").click();
    await expect(b.locator("#call")).toBeVisible({ timeout: 30_000 });
    await expect(b.locator("#end-all")).toBeHidden();                     // attendees can't end it
    await expect(a.locator("#people")).toContainText("Ben Das", { timeout: 15_000 });
    await expect(b.locator("#people")).toContainText("Asha Rao");
    await expect(a.locator("#translator")).not.toContainText("joining", { timeout: 20_000 });

    a.once("dialog", (d) => d.accept());
    await a.locator("#end-all").click();
    await expect(a).toHaveURL(/\/dashboard$/, { timeout: 15_000 });
    await expect(a.locator("#meetings")).toContainText("ended");
    await b.waitForTimeout(3000);
    await expect(b).toHaveURL(/\/dashboard$/, { timeout: 15_000 });
    await expect(b.getByRole("status")).toHaveText("The host ended the meeting.");

  } finally {
    await Promise.all([ba.close(), bb.close()]);
  }
});
