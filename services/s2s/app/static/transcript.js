// A meeting's saved transcript, its AI summary and action items, and questions about it.
"use strict";
const code = location.pathname.split("/").pop();
const fail = (e) => { $("err").textContent = e.message; $("err").hidden = false; };

function renderSummary(s) {
  const box = $("summary");
  if (!s) { box.replaceChildren(el("p", { class: "hint", text: "No summary yet." })); return; }
  box.replaceChildren(
    el("p", { text: s.summary }),
    s.decisions.length ? el("h3", { text: "Decisions" }) : null,
    s.decisions.length ? el("ul", {}, ...s.decisions.map((d) => el("li", { text: d.text }))) : null,
    s.actionItems.length ? el("h3", { text: "Action items" }) : null,
    s.actionItems.length ? el("ul", { class: "actions" }, ...s.actionItems.map((a) => el("li", {},
      el("strong", { text: a.task }),
      el("span", { class: "meta", text: [a.owner && ` · ${a.owner}`, a.due && ` · ${a.due}`].filter(Boolean).join("") })))) : null,
    el("p", { class: "hint", text: `Made from ${s.segmentCount} lines, ${new Date(s.generatedAt).toLocaleString()}.` }));
}

async function load() {
  const d = await api(`/api/meetings/${code}/transcript?lang=${$("lang").value}`);
  document.title = `${d.meeting.title} · MeetMind`;
  $("title").textContent = d.meeting.title;
  $("meta").textContent = [d.meeting.status.toLowerCase(), d.meeting.startedAt && new Date(d.meeting.startedAt).toLocaleString(),
    d.meeting.saveTranscript ? "transcript being saved" : null].filter(Boolean).join(" · ");
  renderSummary(d.summary);
  $("delete").hidden = !(d.meeting.canDelete && (d.segments.length || d.summary));
  const lines = $("lines");
  if (!d.segments.length) {
    lines.replaceChildren(el("li", { class: "meta", text: "Nothing saved. The host turns on “Save transcript” in the call." }));
    return;
  }
  lines.replaceChildren(...d.segments.map((s) => el("li", { id: `l${s.id}` },
    el("span", { class: "meta", text: `${s.at || ""} ` }), el("strong", { text: `${s.speaker}: ` }),
    el("span", { text: $("lang").value && s.translation ? s.translation : s.text }))));
}

$("delete").addEventListener("click", async () => {
  if (!confirm("Delete this meeting's saved transcript, translations and summary for everyone? This can't be undone.")) return;
  $("delete").disabled = true;
  try { await api(`/api/meetings/${code}/transcript`, undefined, "DELETE"); await load(); } catch (e) { fail(e); }
  finally { $("delete").disabled = false; }
});
$("lang").addEventListener("change", () => load().catch(fail));
$("summarise").addEventListener("click", async () => {
  $("summarise").disabled = true;
  $("summarise").textContent = "Summarising…";
  try { renderSummary((await api(`/api/meetings/${code}/summary`)).summary); } catch (e) { fail(e); }
  finally { $("summarise").disabled = false; $("summarise").textContent = "Summarise now"; }
});
$("ask-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = $("question").value.trim();
  if (q.length < 3) return;
  $("answer").replaceChildren(el("p", { class: "hint", text: "Looking through the transcript…" }));
  try { $("answer").replaceChildren(answerView(await api("/api/ask", { question: q, meetingCode: code }))); }
  catch (e) { $("answer").replaceChildren(); fail(e); }
});
load().catch(fail);
