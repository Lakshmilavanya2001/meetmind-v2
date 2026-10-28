// Sign in and create an account; then on to the page that sent you here (or the dashboard).
"use strict";
const $ = (id) => document.getElementById(id);
const next = (() => {
  const n = new URLSearchParams(location.search).get("next") || "/dashboard";
  return n.startsWith("/") && !n.startsWith("//") ? n : "/dashboard";      // only paths on this site
})();

function showTab(signup) {
  $("tab-login").setAttribute("aria-selected", String(!signup));
  $("tab-signup").setAttribute("aria-selected", String(signup));
  $("login-form").hidden = signup;
  $("signup-form").hidden = !signup;
  $("auth-error").hidden = true;
  history.replaceState(null, "", (signup ? "/signup" : "/login") + location.search);
}
$("tab-login").addEventListener("click", () => showTab(false));
$("tab-signup").addEventListener("click", () => showTab(true));
if (location.pathname === "/signup") showTab(true);

document.querySelectorAll('input[name="org-mode"]').forEach((r) => r.addEventListener("change", () => {
  const isNew = document.querySelector('input[name="org-mode"]:checked').value === "new";
  $("org-name-field").hidden = !isNew;
  $("join-code-field").hidden = isNew;
}));

async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = data.detail;
    throw new Error(Array.isArray(d) ? d.map((e) => e.msg.replace(/^Value error, /, "")).join(" ") : d || r.statusText);
  }
  return data;
}

function fail(e) {
  $("auth-error").textContent = e.message;
  $("auth-error").hidden = false;
}

$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await post("/api/auth/login", { email: $("login-email").value, password: $("login-password").value });
    location.assign(next);
  } catch (e) { fail(e); }
});

$("signup-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const isNew = document.querySelector('input[name="org-mode"]:checked').value === "new";
  try {
    await post("/api/auth/signup", {
      name: $("su-name").value, email: $("su-email").value, password: $("su-password").value,
      ...(isNew ? { organization: $("su-org").value } : { join_code: $("su-code").value }),
    });
    location.assign(next);
  } catch (e) { fail(e); }
});
