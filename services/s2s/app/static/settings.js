// Settings: your profile (name, time zone, preferred language), your Uber connection and your Uber rides.
"use strict";
const RESULT = {
  connected: [true, "Uber connected."],
  denied: [false, "Uber sign-in was cancelled."],
  expired: [false, "That Uber sign-in expired. Try again."],
  signin: [false, "Sign in to MeetMind first, then connect Uber."],
  error: [false, "Uber didn't accept the sign-in. Check the Uber app's settings and try again."],
};
const RIDE_FINAL = new Set(["completed", "rider_canceled", "driver_canceled", "no_drivers_available"]);

function fail(e) { $("err").textContent = e.message; $("err").hidden = false; }
function clearError() { $("err").hidden = true; }
function note(ok, text) {
  const n = $("note");
  n.textContent = text;
  n.style.color = ok ? "var(--ok)" : "var(--danger)";
  n.hidden = false;
}
const when = (iso) => iso ? new Date(iso).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";

// ─── Profile ─────────────────────────────────────────────────────────────
function fillSelect(select, options, value) {
  if (value && !options.some(([v]) => v === value)) options.unshift([value, value]);
  select.replaceChildren(...options.map(([v, label]) => el("option", { value: v, text: label })));
  select.value = value;
}

async function loadProfile() {
  const [me, langs] = await Promise.all([api("/api/me"), api("/api/languages").catch(() => ({ hear: [] }))]);
  $("p-name").value = me.user.displayName;
  let zones = [];
  try { zones = Intl.supportedValuesOf("timeZone"); } catch { zones = ["Asia/Kolkata", "UTC"]; }
  fillSelect($("p-tz"), zones.map((z) => [z, z.replace(/_/g, " ")]), me.user.timezone);
  const languages = langs.hear.length ? langs.hear : [{ code: "en", name: "English", native: "English" }];
  fillSelect($("p-lang"), languages.map((l) => [l.code, l.native && l.native !== l.name ? `${l.name} · ${l.native}` : l.name]), me.user.preferredLanguage);
}

$("profile-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  clearError();
  const btn = e.submitter;
  btn.disabled = true;
  try {
    const { user } = await api("/api/me", { displayName: $("p-name").value.trim(), timezone: $("p-tz").value, preferredLanguage: $("p-lang").value }, "PATCH");
    $("me-name").textContent = user.displayName;
    $("profile-saved").textContent = "Saved.";
    setTimeout(() => { $("profile-saved").textContent = ""; }, 3000);
  } catch (err) { fail(err); } finally { btn.disabled = false; }
});

// ─── Uber ────────────────────────────────────────────────────────────────
async function loadUber() {
  const st = await api("/api/integrations/uber");
  const badge = $("uber-badge");
  badge.hidden = !st.env;
  if (st.env === "sandbox") { badge.textContent = "Test mode"; badge.title = "No real car comes and nothing is charged"; badge.className = "badge"; }
  if (st.env === "production") { badge.textContent = "Real rides"; badge.title = "Rides are real and charged"; badge.className = "badge live"; }
  $("uber-about").textContent = "Connect your Uber account and the assistant shows Uber's real options and fares, and books rides after you confirm. "
    + (st.env === "production" ? "Rides are real and charged to your Uber account." : "In test mode no real car comes and nothing is charged.");

  const box = $("uber-state");
  if (!st.configured) {
    box.replaceChildren(el("span", { class: "meta", text: "Uber isn't set up on this server yet. Cab bookings use the sandbox." }));
  } else if (st.connected) {
    const btn = el("button", { class: "btn ghost small", type: "button", text: "Disconnect" });
    btn.addEventListener("click", async () => {
      if (!confirm("Disconnect your Uber account? The assistant will go back to the sandbox for cabs.")) return;
      btn.disabled = true;
      clearError();
      try { await api("/api/integrations/uber/disconnect"); note(true, "Uber disconnected."); await loadUber(); } catch (e) { fail(e); btn.disabled = false; }
    });
    box.replaceChildren(el("span", { text: "✓ Your Uber account is connected." }), btn);
  } else {
    // A full page navigation: the server redirects to Uber's sign-in.
    box.replaceChildren(
      el("span", { text: st.needsReconnect ? "Your Uber login has expired." : "Not connected. Cab bookings use the sandbox." }),
      el("a", { class: "btn primary small", href: "/api/integrations/uber/connect", text: st.needsReconnect ? "Reconnect Uber" : "Connect Uber" }));
  }
  return st;
}

async function rideAction(btn, url, method) {
  btn.disabled = true;
  clearError();
  try { await api(url, method === "POST" ? {} : undefined, method === "POST" ? "POST" : undefined); } catch (e) { fail(e); }
  await loadRides();
}

async function loadRides() {
  const { rides } = await api("/api/rides");
  $("rides-card").hidden = !rides.length;
  $("rides").replaceChildren(...rides.map((r) => {
    const over = r.status !== "CONFIRMED" || RIDE_FINAL.has(r.ride.status);
    const meta = [r.ride.label, r.ride.driver, r.ride.vehicle, r.total, when(r.createdAt), r.uberTest ? "test mode" : null].filter(Boolean).join(" · ");
    const right = el("div", { class: "row" },
      el("span", { class: "badge" + (over ? "" : " live"), text: r.status === "CANCELED" ? "cancelled" : r.ride.status.replace(/_/g, " ") }));
    if (!over) {
      const refresh = el("button", { class: "btn ghost small", type: "button", text: "Refresh" });
      refresh.addEventListener("click", () => rideAction(refresh, `/api/rides/${r.id}`));
      right.append(refresh);
      if (r.uberTest) {
        const next = el("button", { class: "btn ghost small", type: "button", text: "Next stage", title: "Test mode: move the ride to its next stage" });
        next.addEventListener("click", () => rideAction(next, `/api/rides/${r.id}/advance`, "POST"));
        right.append(next);
      }
      if (r.ride.status !== "in_progress") {
        const cancel = el("button", { class: "btn ghost small", type: "button", text: "Cancel" });
        cancel.addEventListener("click", () => {
          if (!confirm(`Cancel ${r.title}?${r.uberTest ? "" : " Uber may charge a cancellation fee."}`)) return;
          rideAction(cancel, `/api/rides/${r.id}/cancel`, "POST");
        });
        right.append(cancel);
      }
    }
    return el("li", {}, el("div", {}, el("div", { text: r.title, style: "font-weight:600" }), el("div", { class: "meta", text: meta })), right);
  }));
}

(async () => {
  const result = new URLSearchParams(location.search).get("uber");
  if (result && RESULT[result]) {
    note(...RESULT[result]);
    history.replaceState(null, "", "/settings");
  }
  try {
    await loadProfile();
    await Promise.all([loadUber(), loadRides()]);
  } catch (e) { fail(e); }
})();
