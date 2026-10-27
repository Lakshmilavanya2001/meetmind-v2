"""End-to-end test of TTS sharing: two listeners hear the same language. Asha (hi)
speaks; Bala (speaks ta) and Chitra (speaks ml) both hear Tamil, and one Tamil synthesis
should reach both (identical TTS stats). Then Chitra speaks. Reuses the callers from
call_test.py and call_test3.py.

    docker run --rm --network host --env-file .env -v $PWD/clients:/clients -v $PWD/testdata:/td \\
        indic-s2s:0.1 python /clients/call_test_shared.py
"""
import argparse, asyncio, time
from call_test import Caller, read_wav, wait_for
from call_test3 import turn

async def main(a):
    room = f"shared-{int(time.time())}"
    asha, bala, chitra = Caller("Asha", "hi", "hi", "female"), Caller("Bala", "ta", "ta", "male"), Caller("Chitra", "ml", "ta", "female")
    for p in (asha, bala, chitra):
        await p.join(a.url, a.api, room)
    assert await wait_for(lambda: any(len(m.get("callers", [])) == 3 for m in asha.msgs("status")), 20)
    await asyncio.sleep(1.0)
    t0 = time.time()
    ok = await turn("Asha (hi) -> Bala (ta), Chitra (ta)", asha, [bala, chitra], read_wav("/td/sample_hindi.wav"))
    mb = [m for m in bala.msgs("metrics", t0) if m["listener"] == bala.identity]
    mc = [m for m in chitra.msgs("metrics", t0) if m["listener"] == chitra.identity]
    same = bool(mb and mc) and all(x["audio_s"] == y["audio_s"] and x["tts_ttfa_ms"] == y["tts_ttfa_ms"] for x, y in zip(mb, mc))
    print("\none synthesis shared (identical TTS stats for both listeners):", same,
          [(m["audio_s"], m["tts_ttfa_ms"]) for m in mb], [(m["audio_s"], m["tts_ttfa_ms"]) for m in mc])
    ok2 = await turn("Chitra (ml) -> Asha (hi), Bala (ta)", chitra, [asha, bala], read_wav("/td/malayalam_tts_16k.wav"))
    for p in (asha, bala, chitra):
        await p.room.disconnect()
    print("\nRESULT:", "PASS" if ok and ok2 and same else "FAIL")

p = argparse.ArgumentParser()
p.add_argument("--url", default="ws://127.0.0.1:7880")
p.add_argument("--api", default="http://127.0.0.1:8090")
asyncio.run(main(p.parse_args()))
