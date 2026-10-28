// Your orders and bookings (made through the assistant): status, total, when, and Cancel.
"use strict";
const KIND = { FOOD: "Food", CAB: "Cab", TABLE: "Table", MOVIE: "Movie" };
const fail = (e) => { $("err").textContent = e.message; $("err").hidden = false; };

function cancellable(b) {
  if (b.status !== "CONFIRMED") return false;
  if (b.uber) return !["in_progress", "completed", "rider_canceled", "driver_canceled", "no_drivers_available"].includes(b.ride?.status);
  const now = Date.now();
  if (b.kind === "FOOD") return now - new Date(b.createdAt).getTime() <= 5 * 60_000;
  return !b.scheduledFor || new Date(b.scheduledFor).getTime() > now;
}

function row(b) {
  const meta = [KIND[b.kind] || b.kind, b.total, b.when, `ref ${b.reference}`, b.stage, b.ride?.label, b.ride?.driver, b.ride?.vehicle].filter(Boolean).join(" · ");
  const right = el("span", { class: "row", style: "align-items:center;gap:8px" },
    b.sandbox ? el("span", { class: "badge", text: "Sandbox" }) : null,
    b.uber ? el("span", { class: "badge", text: b.uberTest ? "Uber test" : "Uber" }) : null,
    el("span", { class: "badge" + (b.status === "CONFIRMED" ? " live" : ""), text: b.status === "CONFIRMED" ? "confirmed" : "cancelled" }));
  if (cancellable(b)) {
    const btn = el("button", { class: "btn ghost small", type: "button", text: "Cancel" });
    btn.addEventListener("click", async () => {
      if (!confirm(`Cancel ${b.title}?`)) return;
      btn.disabled = true;
      try { await api(`/api/bookings/${b.id}/cancel`); await load(); } catch (e) { btn.disabled = false; fail(e); }
    });
    right.append(btn);
  }
  return el("li", {}, el("div", {}, el("strong", { text: b.title }), el("div", { class: "meta", text: meta })), right);
}

async function load() {
  $("err").hidden = true;
  const { bookings } = await api("/api/bookings");
  $("bookings").replaceChildren(...(bookings.length ? bookings.map(row)
    : [el("li", { class: "meta", text: "Nothing yet. Ask the assistant to order food, book a cab, a table or movie tickets." })]));
}

$("refresh").addEventListener("click", () => load().catch(fail));
load().catch(fail);
