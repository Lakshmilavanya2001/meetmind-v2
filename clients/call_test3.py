"""End-to-end test of a translated call with THREE simulated callers, each hearing their
own language. Reuses the callers from call_test.py.

    docker run --rm --network host --env-file .env -v $PWD/clients:/clients -v $PWD/testdata:/td \\
        indic-s2s:0.1 python /clients/call_test3.py

Scenario: Asha (speaks/hears Hindi), Bala (Tamil) and Chitra (Malayalam) take one turn
each; the other two hear every turn in their own language. Then Bala switches to hearing
English and Asha speaks again: one English step is both Bala's translation and the
bridge to Malayalam for Chitra.
"""
import argparse
import asyncio
import time

from livekit import rtc

from call_test import Caller, read_wav, wait_for, write_wav


async def turn(label, speaker, listeners, clip):
    pcm, sr = clip
    t_start = time.time()
    await speaker.say(pcm, sr)
    speech_end = t_start + len(pcm) / sr

    def done():
        finals = [m for m in speaker.msgs("transcript", t_start) if m["final"] and m["text"]]
        return finals and all(
            len([m for m in L.msgs("metrics", t_start) if m["listener"] == L.identity]) >= len(finals)
            for L in listeners)

    ok = await wait_for(done, timeout=60)
    await asyncio.sleep(0.5)
    transcripts = [m["text"] for m in speaker.msgs("transcript", t_start) if m["final"]]
    print(f"\n=== {label}")
    print(f"  {speaker.name} said ({speaker.speak}): {' | '.join(transcripts)}")
    results = [ok]
    for L in listeners:
        audio = L.audio_since(t_start)
        out = f"/td/out/call3_{speaker.name}_to_{L.name}_{L.hear}.wav"
        write_wav(out, audio, 48000)
        first = L.first_speech_since(t_start)
        texts = [m["text"] + (f"  [via English: {m['pivot_text']}]" if m.get("pivot_text") else "")
                 for m in L.msgs("translation", t_start) if m["final"] and m["lang"] == L.hear]
        metrics = [m for m in L.msgs("metrics", t_start) if m["listener"] == L.identity]
        print(f"  -> {L.name} hears ({L.hear}): {' | '.join(texts)}")
        print(f"     audio received {len(audio) / 48000:.1f} s; heard "
              f"{(first - speech_end) * 1000:.0f} ms after the speaker stopped" if first else "     no audio")
        for m in metrics:
            print(f"     agent: translation done {m['translation_done_ms']} ms, speech starts {m['audio_start_ms']} ms")
        results.append(len(audio) > 0)
    return all(results)


async def main(a):
    room = f"test3-{int(time.time())}"
    asha = Caller("Asha", "hi", "hi", "female")
    bala = Caller("Bala", "ta", "ta", "male")
    chitra = Caller("Chitra", "ml", "ml", "female")
    people = [asha, bala, chitra]
    for p in people:
        await p.join(a.url, a.api, room)
    assert await wait_for(lambda: any(len(m.get("callers", [])) == 3 for m in asha.msgs("status")), 20), \
        "agent did not report three callers"
    await asyncio.sleep(1.0)
    for p in people:      # each person may only receive their own translated track
        tracks = [t for _, t, k in p.subscribed if k == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT]
        assert tracks == [f"translation-{p.identity}"], f"{p.name} can hear another translation: {tracks}"
    print("each caller receives only their own translation track: ok")

    clips = {"hi": read_wav(a.hindi), "ta": read_wav(a.tamil), "ml": read_wav(a.malayalam)}
    results = []
    for speaker in people:
        others = [p for p in people if p is not speaker]
        results.append(await turn(f"{speaker.name} ({speaker.speak}) -> " + ", ".join(f"{o.name} ({o.hear})" for o in others),
                                  speaker, others, clips[speaker.speak]))
    await bala.room.local_participant.set_attributes({"speak": "ta", "hear": "en", "voice": "male"})
    bala.hear = "en"
    await asyncio.sleep(1.0)
    results.append(await turn("Asha (hi) -> Bala (now en), Chitra (ml)", asha, [bala, chitra], clips["hi"]))
    for p in people:
        await p.room.disconnect()
    print("\nRESULT:", "PASS" if all(results) else "FAIL", results)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:7880")
    p.add_argument("--api", default="http://127.0.0.1:8090")
    p.add_argument("--hindi", default="/td/sample_hindi.wav")
    p.add_argument("--tamil", default="/td/tamil_16k.wav")
    p.add_argument("--malayalam", default="/td/malayalam_tts_16k.wav")
    asyncio.run(main(p.parse_args()))
