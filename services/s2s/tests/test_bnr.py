"""BNR client: hand-encoded messages, and the stream against a stand-in gRPC server (halves every
sample, so cleaned audio can be told from the original), including the pass-through when it dies."""
import asyncio

import grpc
import numpy as np
import pytest

from app import bnr


def test_messages_round_trip():
    f = (np.sin(np.arange(bnr.BNR_CHUNK) / 10) * 0.5).astype(np.float32)
    assert np.array_equal(bnr.decode_audio(bnr.encode_audio(f)), f)
    cfg = bnr.encode_config(0.75)
    assert cfg[:3] == b"\x12\x05\x0d" and np.frombuffer(cfg[3:], "<f4")[0] == 0.75
    assert bnr.decode_audio(cfg) is None          # a config echo carries no audio
    pcm = np.array([0, 1000, -1000, 32767, -32768], dtype="<i2").tobytes()
    assert bnr.float_to_int16(bnr.int16_to_float(pcm)) == pcm


async def _server():
    async def enhance(request_iterator, context):
        async for b in request_iterator:
            f = bnr.decode_audio(b)
            if f is not None:
                yield bnr.encode_audio(f / 2)

    server = grpc.aio.server()
    handler = grpc.method_handlers_generic_handler("nvidia.ai4m.bnr.v1.BNR", {
        "EnhanceAudio": grpc.stream_stream_rpc_method_handler(enhance, request_deserializer=lambda b: b, response_serializer=lambda b: b)})
    server.add_generic_rpc_handlers((handler,))
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return server, f"127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_cleans_in_10ms_chunks_then_passes_through_when_the_server_dies():
    server, target = await _server()
    out: list[int] = []
    s = bnr.BnrStream(lambda pcm: out.extend(np.frombuffer(pcm, "<i2").tolist()), target=target)
    s.start()
    for _ in range(50):                            # wait for the stream to open
        if s.cleaning:
            break
        await asyncio.sleep(0.02)
    assert s.cleaning
    s.push(np.full(700, 1000, "<i2").tobytes())    # 1.46 chunks: one sent, the rest waits
    s.push(np.full(260, 1000, "<i2").tobytes())    # two full chunks in total
    for _ in range(50):
        if len(out) >= 2 * bnr.BNR_CHUNK:
            break
        await asyncio.sleep(0.02)
    assert len(out) == 2 * bnr.BNR_CHUNK and set(out) == {500}   # "cleaned" (halved) by the server

    await server.stop(None)
    for _ in range(100):
        if not s.cleaning:
            break
        await asyncio.sleep(0.02)
    assert not s.cleaning
    out.clear()
    s.push(np.full(100, 1000, "<i2").tobytes())
    assert out == [1000] * 100                      # the original, at once: the caller isn't lost
    await s.close()
    assert bnr.active_streams == 0
