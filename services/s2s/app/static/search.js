// Questions across your meetings, answered from saved transcripts with their lines.
"use strict";
$("ask-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = $("question").value.trim();
  if (q.length < 3) return;
  $("err").hidden = true;
  $("answer").replaceChildren(el("p", { class: "hint", text: "Looking through your meetings…" }));
  try { $("answer").replaceChildren(answerView(await api("/api/ask", { question: q }))); }
  catch (e) { $("answer").replaceChildren(); $("err").textContent = e.message; $("err").hidden = false; }
});
