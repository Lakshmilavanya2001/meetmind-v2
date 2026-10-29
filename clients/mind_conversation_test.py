"""A conversation with Mind in a call: once woken, Mind keeps listening (no "Hey Mind" again) until
"Thanks Mind", and then the caller's words go to the call again.

Ben (English) asks "Hey Mind, what meetings do I have this week?", then, after the spoken answer, a
follow-up without the wake phrase ("And who else is in this call right now?"), then "Thanks Mind.",
then a normal line. The follow-up and "Thanks" must stay private (Asha gets nothing); the page is told
when the conversation opens and closes; the normal line afterwards reaches Asha, translated.

    docker run --rm --network host -v $PWD/clients:/clients -v $PWD/testdata:/td \\
        indic-s2s:0.1 python /clients/mind_conversation_test.py --url ws://127.0.0.1:7890 --api http://127.0.0.1:8091
"""
import argparse
import asyncio
import time

from call_test import Caller, read_wav
from mind_test import about, trimmed


async def wait_for(caller, pred, since, timeout=40):
    for _ in range(int(timeout * 10)):
        hits = [m for ts, m in caller.messages if ts >= since and pred(m)]
        if hits:
            return hits
        await asyncio.sleep(0.1)
    return []


def session_states(caller, since):
    return [m["active"] for ts, m in caller.messages if ts >= since and m.get("type") == "mind_session"]


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:7880")
    p.add_argument("--api", default="http://127.0.0.1:8090")
    args = p.parse_args()
    room = f"mindconv-{int(time.time())}"
    asha, ben = Caller("Asha", "hi", "hi", "female"), Caller("Ben", "en", "en", "male")
    await asha.join(args.url, args.api, room)
    await ben.join(args.url, args.api, room)
    await asyncio.sleep(6)
    ok = []
    t0 = time.time()

    async def step(label, wav, expect_answer=True, wait_after=1.0):
        t = time.time()
        await ben.say(*trimmed(read_wav(wav)), then_silence_s=1.0)
        heard = await wait_for(ben, lambda m: m.get("type") == "transcript" and m.get("final") and m.get("speaker") == ben.identity, t, 20)
        answer = await wait_for(ben, lambda m: m.get("type") == "assistant" and m.get("state") == "answer", t, 40) if expect_answer else []
        if answer:                       # let the spoken answer play out (the conversation stays open after it)
            await asyncio.sleep(len(answer[0]["text"]) / 14 + 1.5)
        await asyncio.sleep(wait_after)
        leaked = about(asha, ben, t)
        text = heard[-1]["text"] if heard else "(nothing)"
        private = bool(heard and heard[-1].get("assistant"))
        print(f"=== {label}\n  Ben said: {text}  (private: {private})")
        if expect_answer:
            print(f"  Mind ({answer[0].get('ms') if answer else '-'} ms): {answer[0]['text'] if answer else '(no answer)'}")
        print(f"  Asha received about it: {len(leaked)} message(s) {[m.get('text') for m in leaked][:2]}")
        return heard, answer, leaked, private

    _, a1, l1, p1 = await step("1. Hey Mind, a question", "/td/hey-mind.wav")
    ok.append(bool(a1) and not l1 and p1)
    _, a2, l2, p2 = await step("2. Follow-up without 'Hey Mind'", "/td/mind-followup.wav")
    ok.append(bool(a2) and not l2 and p2)
    _, _, l3, p3 = await step("3. 'Okay, stop it, Mind.'", "/td/mind-stop.wav", expect_answer=False, wait_after=3.0)
    ok.append(not l3 and p3)
    states = session_states(ben, t0)
    print(f"  conversation open/closed on Ben's page: {states}")
    ok.append(bool(states) and states[0] is True and states[-1] is False)
    t = time.time()
    await ben.say(*trimmed(read_wav("/td/after-mind.wav")), then_silence_s=2.0)
    got = await wait_for(asha, lambda m: m.get("type") == "translation" and m.get("final") and m.get("speaker") == ben.identity, t, 20)
    print(f"=== 4. Normal line afterwards\n  Asha got: {[m['text'] for m in got]}")
    ok.append(bool(got))
    for c in (asha, ben):
        await c.room.disconnect()
    print("RESULT:", "PASS" if all(ok) else "FAIL", ok)


if __name__ == "__main__":
    asyncio.run(main())
