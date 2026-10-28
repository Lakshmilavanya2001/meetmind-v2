import path from "node:path";
import { chromium, expect, test } from "@playwright/test";
test.setTimeout(240_000);
const BASE = process.env.BASE_URL || "https://10.79.254.169:8444";

// The call screen in meetmind's style: people as circles, round keys with the End key in the middle,
// side panels (People, Assistant, Details), captions on/off, and "Ask Mind".
test("meetmind-v2: call screen, panels and the assistant inside the call", async () => {
  const launch = () => chromium.launch({ args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
    `--use-file-for-fake-audio-capture=${path.resolve("silence.wav")}`, "--autoplay-policy=no-user-gesture-required"] });
  const [ba, bb] = await Promise.all([launch(), launch()]);
  const tag = Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  try {
    const a = await (await ba.newContext({ ignoreHTTPSErrors: true, baseURL: BASE, viewport: { width: 1280, height: 800 } })).newPage();
    const b = await (await bb.newContext({ ignoreHTTPSErrors: true, baseURL: BASE })).newPage();
    const signup = (p, data) => p.request.post("/api/auth/signup", { headers: { origin: BASE }, data: { password: "correct-horse-1", ...data } });
    expect((await signup(a, { name: "Asha Rao", email: `asha-${tag}@v2.test`, organization: `Acme ${tag}` })).ok()).toBeTruthy();
    const org = (await (await a.request.get("/api/me")).json()).organization.joinCode;
    expect((await signup(b, { name: "Ben Das", email: `ben-${tag}@v2.test`, join_code: org })).ok()).toBeTruthy();
    const code = (await (await a.request.post("/api/meetings", { headers: { origin: BASE }, data: { title: "Weekly sync" } })).json()).meeting.joinCode;

    for (const p of [a, b]) { await p.goto(`/m/${code}`); await p.locator("#join-btn").click(); }
    await expect(a.locator("#call")).toBeVisible({ timeout: 30_000 });
    await expect(a.locator(".stage .person")).toHaveCount(2, { timeout: 20_000 });
    await expect(a.locator("#call-title")).toContainText("Ben Das");
    await expect(a.locator("#room-name")).toHaveText("Weekly sync");
    await expect(a.locator("#elapsed")).toHaveText(/^\d\d:\d\d$/);
    await expect(a.locator("#conn-text")).toHaveText("Connected");
    await expect(a.locator("#people-count")).toHaveText("2 people");
    await expect(a.locator("#translator")).not.toContainText("joining", { timeout: 20_000 });
    await expect(a.locator("#leave.key-end")).toBeVisible();
    await expect(a.locator("#end-all")).toBeVisible();          // the host
    await expect(b.locator("#end-all")).toBeHidden();
    await a.screenshot({ path: "call-screen.png" });

    // Muting shows on both screens.
    await b.locator("#mute").click();
    await expect(a.locator(`.person:not(.me).muted .person-state`)).toHaveText("muted", { timeout: 10_000 });

    // Captions on / off.
    await a.locator("#captions-toggle").click();
    await expect(a.locator("#feed")).toBeHidden();
    await a.locator("#captions-toggle").click();
    await expect(a.locator("#feed")).toBeVisible();

    // People panel.
    await a.locator("#panel-people").click();
    await expect(a.locator("#people-list")).toContainText("Ben Das");
    await expect(a.locator("#people-list")).toContainText("muted");

    // The assistant inside the call (real model): attached to this meeting.
    await a.locator("#panel-assistant").click();
    await expect(a.locator("#side-title")).toHaveText("Assistant");
    await a.locator("#side-assistant textarea").fill("Who is in this call right now?");
    await a.locator("#side-assistant button[type=submit]").click();
    await expect(a.locator("#side-assistant .msg.assistant").last()).toBeVisible({ timeout: 60_000 });
    await a.screenshot({ path: "call-assistant.png" });
    const convs = await (await a.request.get(`/api/assistant/conversations?meeting=${code}`)).json();
    expect(convs.conversations.length).toBe(1);

    // The panel's own mic: while it's on, the call mic is muted (Ben sees Asha muted) and a notice says so;
    // turning it off brings the call mic back.
    await b.locator("#mute").click();                                     // Ben unmutes again
    const panelMic = a.locator("#side-assistant .mic");
    await expect(panelMic).toBeVisible();
    await expect(a.locator("#mute")).toHaveAttribute("aria-pressed", "false");
    await panelMic.click();
    await expect(panelMic).toHaveAttribute("aria-pressed", "true", { timeout: 10_000 });
    await expect(a.locator("#mute")).toHaveAttribute("aria-pressed", "true");
    await expect(a.locator("#assistant-banner")).toBeVisible();
    await expect(b.locator(".person:not(.me).muted")).toHaveCount(1, { timeout: 10_000 });
    await a.screenshot({ path: "call-assistant-mic.png" });
    await panelMic.click();
    await expect(panelMic).toHaveAttribute("aria-pressed", "false");
    await expect(a.locator("#mute")).toHaveAttribute("aria-pressed", "false");
    await expect(a.locator("#assistant-banner")).toBeHidden();
    await expect(b.locator(".person:not(.me).muted")).toHaveCount(0, { timeout: 10_000 });

    // "Ask Mind": the agent acknowledges privately (its "Yes?" is spoken to Asha only).
    await a.locator("#ask-mind").click();
    await expect(a.locator(".toast").last()).toContainText("Mind is listening");

    await a.locator("#panel-details").click();
    await expect(a.locator("#d-code")).toHaveText(code);
    await a.locator("#end-all").click();
  } finally {
    await Promise.all([ba.close(), bb.close()]);
  }
});
