"""
Nigeria geography for Pheral Gigs: states, common cities -> state, and helpers.

Matching is deliberately simple and predictable:
    same city  -> "In your city"
    same state -> "In your state"
    remote     -> visible to everyone
"""
import re

NIGERIA_STATES = [
    "Abia", "Adamawa", "Akwa Ibom", "Anambra", "Bauchi", "Bayelsa", "Benue", "Borno",
    "Cross River", "Delta", "Ebonyi", "Edo", "Ekiti", "Enugu", "FCT", "Gombe", "Imo",
    "Jigawa", "Kaduna", "Kano", "Katsina", "Kebbi", "Kogi", "Kwara", "Lagos", "Nasarawa",
    "Niger", "Ogun", "Ondo", "Osun", "Oyo", "Plateau", "Rivers", "Sokoto", "Taraba",
    "Yobe", "Zamfara",
]

STATE_ALIASES = {
    "abuja": "FCT", "fct abuja": "FCT", "fct (abuja)": "FCT", "f.c.t": "FCT", "f.c.t.": "FCT",
    "federal capital territory": "FCT", "abuja fct": "FCT",
    "akwaibom": "Akwa Ibom", "akwa-ibom": "Akwa Ibom",
    "crossriver": "Cross River", "cross-river": "Cross River",
    "nassarawa": "Nasarawa",
}

# lowercase city / area -> canonical state
CITY_TO_STATE = {
    # Rivers
    "port harcourt": "Rivers", "portharcourt": "Rivers", "ph": "Rivers", "obio-akpor": "Rivers",
    "obio akpor": "Rivers", "rumuokoro": "Rivers", "eleme": "Rivers", "bonny": "Rivers", "okrika": "Rivers",
    # FCT
    "abuja": "FCT", "garki": "FCT", "wuse": "FCT", "maitama": "FCT", "asokoro": "FCT", "gwarinpa": "FCT",
    "kubwa": "FCT", "lugbe": "FCT", "jabi": "FCT", "gwagwalada": "FCT", "nyanya": "FCT", "karu": "FCT",
    # Lagos
    "lagos": "Lagos", "ikeja": "Lagos", "lekki": "Lagos", "yaba": "Lagos", "surulere": "Lagos",
    "ikorodu": "Lagos", "victoria island": "Lagos", "vi": "Lagos", "ajah": "Lagos", "ikoyi": "Lagos",
    "festac": "Lagos", "badagry": "Lagos", "epe": "Lagos", "apapa": "Lagos", "maryland": "Lagos",
    # others, by state
    "ibadan": "Oyo", "ogbomosho": "Oyo", "oyo": "Oyo",
    "kano": "Kano", "kaduna": "Kaduna", "zaria": "Kaduna",
    "benin city": "Edo", "benin": "Edo", "auchi": "Edo", "ekpoma": "Edo",
    "enugu": "Enugu", "nsukka": "Enugu",
    "warri": "Delta", "asaba": "Delta", "sapele": "Delta", "ughelli": "Delta", "effurun": "Delta",
    "owerri": "Imo", "orlu": "Imo", "okigwe": "Imo",
    "uyo": "Akwa Ibom", "eket": "Akwa Ibom", "ikot ekpene": "Akwa Ibom",
    "calabar": "Cross River", "ogoja": "Cross River",
    "abeokuta": "Ogun", "sagamu": "Ogun", "ijebu ode": "Ogun", "ota": "Ogun", "sango ota": "Ogun",
    "jos": "Plateau", "bukuru": "Plateau",
    "ilorin": "Kwara", "offa": "Kwara",
    "akure": "Ondo", "ondo": "Ondo", "owo": "Ondo",
    "onitsha": "Anambra", "awka": "Anambra", "nnewi": "Anambra", "nnamdi azikiwe": "Anambra",
    "maiduguri": "Borno", "yola": "Adamawa", "sokoto": "Sokoto", "minna": "Niger", "suleja": "Niger",
    "lokoja": "Kogi", "makurdi": "Benue", "abakaliki": "Ebonyi", "bauchi": "Bauchi", "gombe": "Gombe",
    "katsina": "Katsina", "dutse": "Jigawa", "birnin kebbi": "Kebbi", "gusau": "Zamfara",
    "jalingo": "Taraba", "lafia": "Nasarawa", "keffi": "Nasarawa", "karu nasarawa": "Nasarawa",
    "osogbo": "Osun", "ile-ife": "Osun", "ile ife": "Osun", "ife": "Osun", "ilesa": "Osun",
    "ado-ekiti": "Ekiti", "ado ekiti": "Ekiti", "ikere": "Ekiti",
    "umuahia": "Abia", "aba": "Abia", "yenagoa": "Bayelsa", "damaturu": "Yobe",
}

# shown in the "city" suggestions list on forms
CITY_SUGGESTIONS = [
    "Port Harcourt", "Abuja", "Lagos", "Ibadan", "Kano", "Kaduna", "Benin City", "Enugu", "Warri",
    "Owerri", "Uyo", "Calabar", "Abeokuta", "Jos", "Ilorin", "Akure", "Onitsha", "Awka", "Asaba",
    "Maiduguri", "Yola", "Sokoto", "Minna", "Lokoja", "Makurdi", "Abakaliki", "Osogbo", "Ado-Ekiti",
    "Umuahia", "Yenagoa", "Lekki", "Ikeja", "Yaba", "Garki", "Wuse", "Gwarinpa", "Obio-Akpor",
]


def state_label(state):
    return "FCT (Abuja)" if state == "FCT" else state


STATE_CHOICES = [(s, state_label(s)) for s in NIGERIA_STATES]


def _clean(value):
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def normalize_state(value):
    """Canonical state name for what a person typed/selected, or "" if it isn't one."""
    key = _clean(value)
    if not key:
        return ""
    if key in STATE_ALIASES:
        return STATE_ALIASES[key]
    for state in NIGERIA_STATES:
        if state.lower() == key:
            return state
    return ""


def guess_state(city):
    """Best-effort state for a well-known city/area, or ""."""
    return CITY_TO_STATE.get(_clean(city), "")


def state_slug(state):
    """Channel-group-safe slug: 'Akwa Ibom' -> 'akwa-ibom'."""
    return re.sub(r"[^a-z0-9]+", "-", (state or "").lower()).strip("-")