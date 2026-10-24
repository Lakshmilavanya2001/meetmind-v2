"""Scripted microphone files for clients/browser_test.js (Chrome's fake audio capture).

Caller A: 5 s silence, Hindi clip, 25 s silence, Hindi clip again, 8 s silence.
Caller B: 19 s silence, Tamil clip, 25 s silence.
Chrome starts the file when the page opens the mic, i.e. when each caller joins.
"""
import sys

import numpy as np
import soundfile as sf
import soxr

hindi, tamil, out_a, out_b = sys.argv[1:5]
SR = 48000


def load(p):
    w, sr = sf.read(p, dtype="float32")
    return soxr.resample(w, sr, SR) if sr != SR else w


def silence(s):
    return np.zeros(int(SR * s), np.float32)


h, t = load(hindi), load(tamil)
sf.write(out_a, np.concatenate([silence(5), h, silence(25), h, silence(8)]), SR, subtype="PCM_16")
sf.write(out_b, np.concatenate([silence(19), t, silence(25)]), SR, subtype="PCM_16")
print("mic files written")
