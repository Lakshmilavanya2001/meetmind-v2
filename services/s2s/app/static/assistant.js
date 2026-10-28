// The Assistant page: the conversation list, and the chat (assistant-chat.js) with voice.
"use strict";
const params = new URLSearchParams(location.search);
const meetingCode = params.get("meeting");

const chat = new AssistantChat($("chat"), {
  meetingCode,
  conversationId: params.get("c"),
  voice: true,
  onConversation: (id) => { history.replaceState(null, "", `/assistant?c=${id}`); },
  onList: () => loadList().catch(() => {}),
});

async function loadList() {
  const { conversations } = await api("/api/assistant/conversations");
  $("convs").replaceChildren(...conversations.map((c) => {
    const a = el("a", { href: `/assistant?c=${c.id}`, class: c.id === chat.conversationId ? "on" : "", text: c.title });
    a.addEventListener("click", (ev) => { ev.preventDefault(); openConversation(c.id); });
    return el("li", {}, a, c.meeting ? el("div", { class: "meta", text: `in ${c.meeting.title}` }) : null);
  }));
}

async function openConversation(id) {
  history.replaceState(null, "", id ? `/assistant?c=${id}` : `/assistant${meetingCode ? `?meeting=${meetingCode}` : ""}`);
  await chat.open(id);
  loadList().catch(() => {});
}

$("new-chat").addEventListener("click", () => openConversation(null));
openConversation(chat.conversationId).catch((e) => chat.fail(e));
