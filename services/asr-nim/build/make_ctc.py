"""Turn AI4Bharat's IndicConformer hybrid checkpoint (multi-softmax, all 22 languages'
heads) into a stock NeMo CTC model for ONE language, so NVIDIA's nemo2riva can export it.

    python3 make_ctc.py <source.nemo> <lang> <out.nemo>

The CTC head of the source has 5632 + 1 outputs: 22 contiguous slices of 256 tokens
(in the tokenizer's language order) and the blank last. The new model keeps the
encoder and preprocessor as they are and only that language's 256 rows + blank.
"""
import os
import sys
import tarfile
import tempfile

import torch
import yaml
from omegaconf import OmegaConf

src, lang, out = sys.argv[1], sys.argv[2], sys.argv[3]
tmp = tempfile.mkdtemp()
with tarfile.open(src) as t:
    t.extractall(tmp)
cfg = yaml.safe_load(open(os.path.join(tmp, "model_config.yaml")))
langs = list(cfg["tokenizer"]["langs"])
i = langs.index(lang)
lo, hi = i * 256, (i + 1) * 256
tok = cfg["tokenizer"]["langs"][lang]
tokdir = os.path.join(tmp, f"tokenizer_{lang}")
os.makedirs(tokdir)
for key, name in (("model_path", "tokenizer.model"), ("vocab_path", "vocab.txt"), ("spe_tokenizer_vocab", "tokenizer.vocab")):
    os.rename(os.path.join(tmp, tok[key].split(":", 1)[1]), os.path.join(tokdir, name))

vocab = cfg["aux_ctc"]["decoder"]["vocabulary"][lo:hi]
pre = dict(cfg["preprocessor"], dither=0.0)          # inference: no dither
new = {
    "sample_rate": 16000,
    "target": "nemo.collections.asr.models.ctc_bpe_models.EncDecCTCModelBPE",
    "tokenizer": {"dir": tokdir, "type": "bpe"},
    "preprocessor": pre,
    "encoder": cfg["encoder"],
    "decoder": {"_target_": "nemo.collections.asr.modules.ConvASRDecoder", "feat_in": 512,
                "num_classes": 256, "vocabulary": vocab},
    "decoding": {"strategy": "greedy_batch"},
    "train_ds": None, "validation_ds": None, "test_ds": None, "optim": None,
}
for k in ("dropout", "dropout_pre_encoder", "dropout_att"):
    new["encoder"][k] = 0.0

from nemo.collections.asr.models import EncDecCTCModelBPE  # noqa: E402

model = EncDecCTCModelBPE(cfg=OmegaConf.create(new))
sd = torch.load(os.path.join(tmp, "model_weights.ckpt"), map_location="cpu", weights_only=False)
keep = {k: v for k, v in sd.items() if k.startswith(("preprocessor.", "encoder."))}
rows = list(range(lo, hi)) + [5632]                   # the language's tokens, then blank
keep["decoder.decoder_layers.0.weight"] = sd["ctc_decoder.decoder_layers.0.weight"][rows].clone()
keep["decoder.decoder_layers.0.bias"] = sd["ctc_decoder.decoder_layers.0.bias"][rows].clone()
missing, unexpected = model.load_state_dict(keep, strict=False)
from omegaconf import open_dict  # noqa: E402
with open_dict(model.cfg):       # transcribe() reads a couple of keys from here
    model.cfg.validation_ds = {"manifest_filepath": None, "sample_rate": 16000, "batch_size": 4,
                               "shuffle": False, "use_start_end_token": False}
print("missing:", missing, "unexpected:", unexpected)
model.save_to(out)
print("saved", out, "vocab", len(vocab), vocab[:6])
