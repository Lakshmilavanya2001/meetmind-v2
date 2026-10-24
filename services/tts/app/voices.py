"""Voice library for bodhan-ai/indic-speak (from the model's voices.md)."""

# language -> (female, male) recommended voices
DEFAULT_VOICES: dict[str, tuple[str, str]] = {
    "as": ("Prastuti", "Ankur"), "bn": ("Ishita", "Sourav"), "brx": ("Gwrbw", "Sansuma"),
    "doi": ("Preeti", "Sham"), "gu": ("Dhara", "Parth"), "hi": ("Kavya", "Amit"),
    "kn": ("Deepika", "Adarsh"), "ks": ("Zoon", "Ishfaq"), "kok": ("Anjali", "Sandeep"),
    "mai": ("Vaidehi", "Madhukar"), "ml": ("Lakshmi", "Kiran"), "mni": ("Thoibi", "Chaoba"),
    "mr": ("Anagha", "Chinmay"), "ne": ("Srijana", "Sagar"), "or": ("Itishree", "Akash"),
    "pa": ("Kaur", "Manpreet"), "sa": ("Bharati", "Aryaman"), "sat": ("Phulmani", "Sibu"),
    "sd": ("Moomal", "Rano"), "ta": ("Anitha", "Arun"), "te": ("Sravani", "Vamsi"),
    "ur": ("Saba", "Zaid"), "en": ("Kavya", "Amit"),
}

# accept the translate service's FLORES codes too
FLORES_TO_ISO = {
    "asm_Beng": "as", "ben_Beng": "bn", "brx_Deva": "brx", "doi_Deva": "doi", "guj_Gujr": "gu",
    "hin_Deva": "hi", "kan_Knda": "kn", "kas_Arab": "ks", "gom_Deva": "kok", "mai_Deva": "mai",
    "mal_Mlym": "ml", "mni_Mtei": "mni", "mni_Beng": "mni", "mar_Deva": "mr", "npi_Deva": "ne",
    "ory_Orya": "or", "pan_Guru": "pa", "san_Deva": "sa", "sat_Olck": "sat", "snd_Deva": "sd",
    "snd_Arab": "sd", "tam_Taml": "ta", "tel_Telu": "te", "urd_Arab": "ur", "eng_Latn": "en",
    "gom": "kok",
}

ALL_SPEAKERS = sorted({v for pair in DEFAULT_VOICES.values() for v in pair} | {
    # English roster and Bhili voices listed in the model card
    "Bhima", "Dhulji", "Govind", "Jhamku", "Kanku", "Sarju", "Tantya"})

STYLES = ["advertisements", "AIR style news", "TV style news", "single person narration audiobook",
          "children's stories", "educational lecture", "news", "Customer Care",
          "happy", "sad", "anger", "fear", "surprise", "disgust"]


def normalize_language(lang: str | None) -> str | None:
    if not lang:
        return None
    return FLORES_TO_ISO.get(lang, lang.lower())


def pick_speaker(language: str | None, speaker: str | None, gender: str | None) -> str:
    if speaker:
        return speaker      # unseen names don't fail, they just sound averaged (model card)
    lang = normalize_language(language) or "hi"
    female, male = DEFAULT_VOICES.get(lang, DEFAULT_VOICES["hi"])
    return male if (gender or "").lower() in ("m", "male") else female
