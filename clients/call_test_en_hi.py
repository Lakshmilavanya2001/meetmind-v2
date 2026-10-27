"""English <-> Hindi call on the shared model servers (docker-compose.shared.yml: Parakeet,
Riva Translate 1.6B, Magpie). Asha speaks and hears Hindi, Ben speaks and hears English; each
hears the other translated. Reuses the callers from call_test.py.
    docker run --rm --network host -v $PWD/clients:/clients -v $PWD/testdata:/td \\
        indic-s2s:0.1 python /clients/call_test_en_hi.py
"""
import argparse, asyncio, time
import numpy as np
from livekit import rtc
from call_test import Caller, read_wav, turn, wait_for


def trimmed(clip):
    """Without the silence around the speech, so 'speaker stopped' is the end of the clip."""
    pcm, sr = clip
    loud = np.flatnonzero(np.abs(pcm) > 500)
    return (pcm[max(0, loud[0] - sr // 10): loud[-1] + sr // 10], sr) if len(loud) else clip


async def main(a):
    room = f"en-hi-{int(time.time())}"
    A, B = Caller("Asha", "hi", "hi", "female"), Caller("Ben", "en", "en", "male")
    for p in (A, B):
        await p.join(a.url, a.api, room)
    assert await wait_for(lambda: any(len(m.get("callers", [])) == 2 for m in A.msgs("status")), 20), \
        "the translator did not join"
    await asyncio.sleep(1.0)
    agent_tracks_b = [t for i, t, k in B.subscribed if k == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT]
    assert agent_tracks_b == [f"translation-{B.identity}"], f"B can hear someone else's translation: {agent_tracks_b}"
    results = [await turn("Asha (Hindi) -> Ben hears English", A, B, trimmed(read_wav(a.hindi)), "/td/out/en_hi_A_to_B.wav"),
               await turn("Ben (English) -> Asha hears Hindi", B, A, trimmed(read_wav(a.english)), "/td/out/en_hi_B_to_A.wav")]
    for p in (A, B):
        await p.room.disconnect()
    print("\nRESULT:", "PASS" if all(results) else "FAIL", results)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:7880")
    p.add_argument("--api", default="http://127.0.0.1:8090")
    p.add_argument("--hindi", default="/td/sample_hindi.wav")
    p.add_argument("--english", default="/td/english_16k.wav")
    asyncio.run(main(p.parse_args()))
