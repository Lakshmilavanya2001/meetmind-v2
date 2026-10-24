"""Languages the call can use, from what each model service supports.

"speak" languages are the ones with an IndicConformer model in the ASR NIM (Malayalam,
Hindi, Kannada, Telugu, Tamil; services/asr-nim/build adds more). "hear" languages are what sarvam-translate can target and indic-speak can voice:
the 22 scheduled Indian languages plus English. TTS quality tiers come from the
indic-speak model card.
"""

LANGUAGES = {
    # code: (English name, native name, TTS tier)
    "as": ("Assamese", "অসমীয়া", "production"),
    "bn": ("Bengali", "বাংলা", "production"),
    "brx": ("Bodo", "बड़ो", "preview"),
    "doi": ("Dogri", "डोगरी", "preview"),
    "gu": ("Gujarati", "ગુજરાતી", "production"),
    "hi": ("Hindi", "हिन्दी", "production"),
    "kn": ("Kannada", "ಕನ್ನಡ", "production"),
    "kok": ("Konkani", "कोंकणी", "preview"),
    "ks": ("Kashmiri", "کٲشُر", "preview"),
    "mai": ("Maithili", "मैथिली", "preview"),
    "ml": ("Malayalam", "മലയാളം", "production"),
    "mni": ("Manipuri", "মৈতৈলোন্", "preview"),
    "mr": ("Marathi", "मराठी", "production"),
    "ne": ("Nepali", "नेपाली", "preview"),
    "or": ("Odia", "ଓଡ଼ିଆ", "production"),
    "pa": ("Punjabi", "ਪੰਜਾਬੀ", "production"),
    "sa": ("Sanskrit", "संस्कृतम्", "preview"),
    "sat": ("Santali", "ᱥᱟᱱᱛᱟᱲᱤ", "preview"),
    "sd": ("Sindhi", "सिन्धी", "preview"),
    "ta": ("Tamil", "தமிழ்", "production"),
    "te": ("Telugu", "తెలుగు", "production"),
    "ur": ("Urdu", "اردو", "production"),
    "en": ("English", "English", "production"),
}

# Languages with a speech recognition model in the ASR NIM. Riva serves each under its own
# language code (ml-IN, hi-IN, ...); asr.py adds the region.
ASR_LANGUAGES = {"hi", "kn", "ml", "ta", "te"}
SPEAK = [code for code in LANGUAGES if code in ASR_LANGUAGES]
HEAR = list(LANGUAGES)

# The target-language names sarvam-translate's prompt takes: its model card lists the
# plain English names, the same as the first column above.
PROMPT_NAME = {code: v[0] for code, v in LANGUAGES.items()}


def describe() -> dict:
    return {
        "speak": [{"code": c, "name": LANGUAGES[c][0], "native": LANGUAGES[c][1]} for c in SPEAK],
        "hear": [{"code": c, "name": LANGUAGES[c][0], "native": LANGUAGES[c][1],
                  "preview": LANGUAGES[c][2] == "preview"} for c in HEAR],
    }
