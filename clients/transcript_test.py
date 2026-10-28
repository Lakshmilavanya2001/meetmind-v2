"""Saved transcript end to end, on the real models: the host turns saving on, Ben speaks part of a
product-review monologue (English) and Asha a Hindi line, the host ends the meeting; then the
automatic summary and a cited answer to a question about the meeting.

    docker run --rm --network host --env-file .env -v $PWD/clients:/clients -v $PWD/testdata:/td \\
        meetmind-v2-s2s python /clients/transcript_test.py --url ws://127.0.0.1:7890 --api http://127.0.0.1:8091
"""
import argparse
import asyncio
import http.cookiejar
import json
import secrets
import time
import urllib.request

import numpy as np

import call_test
from call_test import Caller, read_wav


class Account:
    def __init__(self, api):
        self.api = api
        self.web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, path, payload=None):
        req = urllib.request.Request(f"{self.api}{path}", json.dumps(payload).encode() if payload is not None else None,
                                     {"content-type": "application/json"})
        return json.load(self.web.open(req))


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:7880")
    p.add_argument("--api", default="http://127.0.0.1:8090")
    args = p.parse_args()
    host, guest = Account(args.api), Account(args.api)
    tag = secrets.token_hex(3)
    host.call("/api/auth/signup", {"name": "Asha", "email": f"asha-{tag}@tt.test", "password": secrets.token_urlsafe(12), "organization": f"Transcript test {tag}"})
    code = host.call("/api/me")["organization"]["joinCode"]
    guest.call("/api/auth/signup", {"name": "Ben", "email": f"ben-{tag}@tt.test", "password": secrets.token_urlsafe(12), "join_code": code})
    meeting = host.call("/api/meetings", {"title": "Weekly product review"})["meeting"]["joinCode"]
    accounts = {"Asha": host, "Ben": guest}
    call_test.token = lambda api, name, room, speak, hear, voice, noise="standard": accounts[name].call(
        f"/api/meetings/{meeting}/join", {"speak": speak, "hear": hear, "voice": voice, "noise": noise})
    asha, ben = Caller("Asha", "hi", "hi", "female"), Caller("Ben", "en", "en", "male")
    await asha.join(args.url, args.api, "x")
    await ben.join(args.url, args.api, "x")
    print("saving:", host.call(f"/api/meetings/{meeting}/transcript", {"enabled": True}))
    await asyncio.sleep(8)                                  # the agent joins and sees the setting
    mono, sr = read_wav("/td/monologue-en.wav")
    await ben.say(mono[: 42 * sr], sr, then_silence_s=2)
    hindi, hsr = read_wav("/td/sample_hindi.wav")
    await asha.say(hindi, hsr, then_silence_s=4)
    host.call(f"/api/meetings/{meeting}/end", {})
    for c in (asha, ben):
        await c.room.disconnect()
    t0 = time.time()
    for _ in range(60):                                    # the watchdog summarises it (every 5 s)
        d = host.call(f"/api/meetings/{meeting}/transcript?lang=hi")
        if d["summary"]:
            break
        await asyncio.sleep(3)
    print(f"\nsaved lines: {len(d['segments'])}")
    for sg in d["segments"][:4]:
        print(f"  [{sg['at']}] {sg['speaker']} ({sg['language']}): {sg['text'][:90]} | hi: {(sg['translation'] or '-')[:50]}")
    s = d["summary"]
    print(f"\nsummary after {time.time() - t0:.0f} s: {s['summary'] if s else '(none)'}")
    for a in (s or {}).get("actionItems", []):
        print(f"  action: {a['task']} | owner: {a['owner'] or '-'} | due: {a['due'] or '-'}")
    await asyncio.sleep(6)                                 # the index follows
    for q in ["When is the mobile release?", "Who will share the customer feedback report, and by when?"]:
        r = host.call("/api/ask", {"question": q})
        print(f"\nQ: {q}\nA: {r['answer']}")
        for c in r["citations"]:
            print(f"   cite [{c['time']}] {c['speaker']}: {c['line'][:90]}")


if __name__ == "__main__":
    asyncio.run(main())
