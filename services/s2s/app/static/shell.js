// The app shell for signed-in pages: the sidebar (brand, navigation, who's signed in) and, on phones, a
// top bar with a menu button. A page marks its place with <nav id="app-nav" data-page="…"></nav>.
"use strict";
(() => {
  const ICONS = {
    logo: '<path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h7A2.5 2.5 0 0 1 16 6.5v4a2.5 2.5 0 0 1-2.5 2.5H9l-3.5 3v-3A2.5 2.5 0 0 1 4 10.5z"/><path d="M19 9.5a1.5 1.5 0 0 1 1 1.4v4.6a2.5 2.5 0 0 1-2.5 2.5H17v2.5L13.8 18H11.5"/>',
    dashboard: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/>',
    assistant: '<path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9z"/><path d="M19 15l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
    bookings: '<path d="M4 7h16v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><path d="M8 7V5a4 4 0 0 1 8 0v2"/>',
    reminders: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5M21 12H9"/>',
    menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  };
  const svg = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;
  const NAV = [
    ["dashboard", "/dashboard", "Meetings"],
    ["assistant", "/assistant", "Assistant"],
    ["search", "/search", "Search"],
    ["bookings", "/bookings", "Bookings"],
    ["reminders", "/reminders", "Reminders"],
    ["settings", "/settings", "Settings"],
  ];
  const nav = document.getElementById("app-nav");
  if (!nav) return;
  const page = nav.dataset.page;
  nav.classList.add("sidebar");
  nav.setAttribute("aria-label", "Main");
  nav.innerHTML = `
    <a class="side-brand" href="/dashboard"><span class="logo">${svg("logo")}</span><span class="side-brand-text">MeetMind<span id="org-name" class="side-org"></span></span></a>
    <ul class="side-nav">${NAV.map(([key, href, label]) => `
      <li><a href="${href}"${key === page ? ' aria-current="page"' : ""}>${svg(key)}<span>${label}</span></a></li>`).join("")}
    </ul>
    <div class="side-user">
      <span class="side-avatar" id="me-avatar" aria-hidden="true"></span>
      <span class="side-user-text"><span id="me-name" class="side-name"></span><span id="me-email" class="side-email"></span></span>
      <button id="logout" class="icon-btn" type="button" aria-label="Sign out" title="Sign out">${svg("logout")}</button>
    </div>`;

  // phones: a top bar with the brand and a menu button that slides the sidebar in
  const bar = document.createElement("header");
  bar.className = "mobile-bar";
  bar.innerHTML = `<button class="icon-btn" type="button" aria-label="Menu" aria-expanded="false" aria-controls="app-nav">${svg("menu")}</button>
    <a class="side-brand" href="/dashboard"><span class="logo">${svg("logo")}</span><span>MeetMind</span></a>`;
  const scrim = document.createElement("div");
  scrim.className = "nav-scrim";
  nav.before(bar);
  nav.after(scrim);
  const menu = bar.querySelector("button");
  const setOpen = (open) => { document.body.classList.toggle("nav-open", open); menu.setAttribute("aria-expanded", String(open)); };
  menu.addEventListener("click", () => setOpen(!document.body.classList.contains("nav-open")));
  scrim.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setOpen(false); });

  const initials = (name) => (name || "?").trim().split(/\s+/).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
  const fill = (me) => {
    document.getElementById("me-name").textContent = me.user.displayName;
    document.getElementById("me-email").textContent = me.user.email || "";
    document.getElementById("me-avatar").textContent = initials(me.user.displayName);
    document.getElementById("org-name").textContent = me.organization?.name || "";
  };
  fetch("/api/me").then((r) => (r.ok ? r.json() : null)).then((me) => me && fill(me)).catch(() => {});
  // the settings page renames you: keep the avatar in step with the name
  new MutationObserver(() => {
    document.getElementById("me-avatar").textContent = initials(document.getElementById("me-name").textContent);
  }).observe(document.getElementById("me-name"), { childList: true, characterData: true, subtree: true });

  document.getElementById("logout").addEventListener("click", async () => {
    try { await fetch("/api/auth/logout", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" }); }
    finally { location.assign("/login"); }
  });
})();
