""""Hey Mind" in a call: the question stays private and the asker gets an answer.

Ben (English) says a normal line, which Asha hears in Hindi. Then Asha asks in Hindi
"हे माइंड इस कॉल में कौन कौन हैं?" (who is in this call?) and Ben asks in English "Hey Mind, what
meetings do I have this week?". Each question must reach only its asker: no caption and no
translation for the other person; the asker gets Mind's answer.

    docker run --rm --network host --env-file .env -v $PWD/clients:/clients -v $PWD/testdata:/td \\
        meetmind-v2-s2s python /clients/mind_test.py --url ws://127.0.0.1:7890 --api http://127.0.0.1:8091
"""
import argparse
import asyncio
import time

import numpy as np

from call_test import Caller, read_wav


def trimmed(wav):
    pcm, sr = wav
    loud = np.where(np.abs(pcm.astype(np.int32)) > 800)[0]
    return pcm[max(0, loud[0] - sr // 4): loud[-1] + sr // 4], sr


def about(caller, speaker, since):
    return [m for ts, m in caller.messages if ts >= since and m.get("speaker") == speaker.identity
            and m.get("type") in ("transcript", "translation", "speech_start_text")]


async def ask(label, asker, other, wav):
    t0 = time.time()
    await asker.say(*trimmed(read_wav(wav)), then_silence_s=1.0)
    for _ in range(150):
        answers = [m for ts, m in asker.messages if ts >= t0 and m.get("type") == "assistant" and m.get("state") == "answer"]
        if answers:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(2)
    heard = [m for ts, m in asker.messages if ts >= t0 and m.get("type") == "transcript" and m.get("final") and m.get("speaker") == asker.identity]
    leaked = about(other, asker, t0)
    print(f"=== {label}")
    print(f"  {asker.name} said: {heard[-1]['text'] if heard else '(nothing)'}  (private: {bool(heard and heard[-1].get('assistant'))})")
    print(f"  Mind answered ({answers[0].get('ms') if answers else '-'} ms): {answers[0]['text'] if answers else '(no answer)'}")
    print(f"  {other.name} received about it: {len(leaked)} message(s) {[m.get('text') for m in leaked][:3]}")
    return bool(answers) and not leaked and bool(heard and heard[-1].get("assistant"))


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:7880")
    p.add_argument("--api", default="http://127.0.0.1:8090")
    args = p.parse_args()
    room = f"mind-{int(time.time())}"
    asha, ben = Caller("Asha", "hi", "hi", "female"), Caller("Ben", "en", "en", "male")
    await asha.join(args.url, args.api, room)
    await ben.join(args.url, args.api, room)
    await asyncio.sleep(6)
    t0 = time.time()
    await ben.say(*trimmed(read_wav("/td/english_16k.wav")), then_silence_s=3.0)
    await asyncio.sleep(2)
    got = [m["text"] for ts, m in asha.messages if ts >= t0 and m.get("type") == "translation" and m.get("final")]
    print(f"=== normal line: Asha got {got}")
    ok = [await ask("Asha asks in Hindi", asha, ben, "/td/caller-hi-heymind.wav"),
          await ask("Ben asks in English", ben, asha, "/td/hey-mind.wav")]
    for c in (asha, ben):
        await c.room.disconnect()
    print("RESULT:", "PASS" if all(ok) and got else "FAIL", ok)


if __name__ == "__main__":
    asyncio.run(main())
