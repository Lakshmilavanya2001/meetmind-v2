import { chromium, expect, test } from "@playwright/test";
test.setTimeout(240_000);
const BASE = process.env.BASE_URL || "https://10.79.254.169:8444";

// The assistant page with the real model: a scheduling request waits for Confirm, then happens;
// the conversation is listed and reopens with its confirmed card.
test("meetmind-v2: assistant schedules a meeting only after Confirm", async () => {
  const browser = await chromium.launch();
  const tag = Date.now().toString(36);
  try {
    const ctx = await browser.newContext({ ignoreHTTPSErrors: true, baseURL: BASE });
    const a = await ctx.newPage();
    const signup = async (page, name, email, extra) => page.request.post("/api/auth/signup", {
      headers: { origin: BASE }, data: { name, email, password: "correct-horse-1", ...extra } });
    expect((await signup(a, "Asha Rao", `asha-${tag}@v2.test`, { organization: `Acme ${tag}` })).ok()).toBeTruthy();
    const code = (await (await a.request.get("/api/me")).json()).organization.joinCode;
    const ben = await (await browser.newContext({ ignoreHTTPSErrors: true, baseURL: BASE })).newPage();
    expect((await signup(ben, "Ben Das", `ben-${tag}@v2.test`, { join_code: code })).ok()).toBeTruthy();

    await a.goto("/dashboard");
    await a.getByRole("link", { name: "Assistant" }).click();
    await expect(a).toHaveURL(/\/assistant$/);
    await a.getByLabel("Message").fill("Schedule a planning meeting with Ben tomorrow at 3 pm");
    await a.getByRole("button", { name: "Send" }).click();
    const card = a.locator(".confirm").first();
    await expect(card).toContainText("Ben Das", { timeout: 120_000 });
    await expect(card).toContainText("Schedule");
    expect((await (await a.request.get("/api/meetings")).json()).meetings.length).toBe(0);   // nothing yet

    await card.getByRole("button", { name: "Confirm" }).click();
    await expect(a.locator(".msg.assistant").last()).toContainText("Done: Scheduled", { timeout: 30_000 });
    await expect(card).toContainText("Confirmed");
    const meetings = (await (await a.request.get("/api/meetings")).json()).meetings;
    expect(meetings.length).toBe(1);

    await a.reload();
    await expect(a.locator(".convs li")).toHaveCount(1);
    await expect(a.locator(".confirm").first()).toContainText("Confirmed");
    await expect(a.locator(".tool.done").first()).toBeVisible();

    // Ben sees the meeting on his dashboard.
    await ben.goto("/dashboard");
    await expect(ben.getByText(meetings[0].title)).toBeVisible();
  } finally {
    await browser.close();
  }
});
