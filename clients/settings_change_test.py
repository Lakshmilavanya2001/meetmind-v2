"""Captions keep coming after a caller changes settings mid-call.

Changing the language (or Studio noise removal) restarts that caller's speech recognition. Ben says a
line, switches English -> Hindi -> English, then says another line: Asha must get a translation for
it under a NEW utterance id (a reused id makes the page update an old caption instead of showing one).

    docker run --rm --network host -v $PWD/clients:/clients -v $PWD/testdata:/td \\
        indic-s2s:0.1 python /clients/settings_change_test.py --url ws://127.0.0.1:7890 --api http://127.0.0.1:8091
"""
import argparse
import asyncio
import time

from call_test import Caller, read_wav
from mind_test import trimmed


async def line(asha, ben, wav):
    t = time.time()
    await ben.say(*trimmed(read_wav(wav)), then_silence_s=2.0)
    for _ in range(200):
        got = [m for ts, m in asha.messages if ts >= t and m.get("type") == "translation" and m.get("final") and m.get("speaker") == ben.identity]
        if got:
            return got[-1]
        await asyncio.sleep(0.1)
    return None


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:7880")
    p.add_argument("--api", default="http://127.0.0.1:8090")
    args = p.parse_args()
    room = f"settings-{int(time.time())}"
    asha, ben = Caller("Asha", "hi", "hi", "female"), Caller("Ben", "en", "en", "male")
    await asha.join(args.url, args.api, room)
    await ben.join(args.url, args.api, room)
    await asyncio.sleep(6)
    first = await line(asha, ben, "/td/english_16k.wav")
    print(f"before: {first and first['utt']} -> {first and first['text']}")
    for lang in ("hi", "en"):                                   # change language twice, as in the settings sheet
        await ben.room.local_participant.set_attributes({"speak": lang, "hear": "en", "voice": "male"})
        await asyncio.sleep(3)
    second = await line(asha, ben, "/td/after-mind.wav")
    print(f"after:  {second and second['utt']} -> {second and second['text']}")
    ok = bool(first and second and first["utt"] != second["utt"])
    for c in (asha, ben):
        await c.room.disconnect()
    print("RESULT:", "PASS" if ok else "FAIL")


if __name__ == "__main__":
    asyncio.run(main())
