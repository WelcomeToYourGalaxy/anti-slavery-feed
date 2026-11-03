#!/usr/bin/env python3
"""
harvest_slavery.py — the Global Wire for forced labour, modern slavery, human
trafficking and child labour, worldwide.

Self-contained: fetching, feed parsing, word-edge matching and deduplication are
all in this file. Reads sources_slavery.json, writes wire_slavery.json.
Standard library only — no dependencies, no API keys, no model calls.

Two directions, one feed. Harm is reported — workers trafficked, wages withheld,
passports taken, children in hazardous work, crews abandoned at sea — and action
is taken — raids and rescues, arrests and convictions, import bans and seized
shipments, court rulings, back wages and compensation, laws passed. Both are
marked, and either can be read alone.

The subject definition (the gate, the refusals, the topic threads and the feed
list) is carried over from the anti-slavery map's own wire: harvest_wire.py and
WIRE_FEEDS, WIRE_THREADS, WIRE_OFF, WIRE_STOP and WIRE_GLOBAL in its index.html.

    python3 harvest_slavery.py
    python3 harvest_slavery.py --dry-run
    python3 harvest_slavery.py --fixtures DIR
    python3 harvest_slavery.py --from-json PATH   # re-tag an existing wire.json
"""

import argparse
import gzip
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# The shared gazetteer, the same one the other feeds use. Optional at import so
# a harvest still runs if the data file is missing; placement then falls back
# to this file's own country table.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import galaxy_places
    _GAZETTEER = True
except Exception as _exc:                       # noqa: BLE001
    print("  ! gazetteer unavailable (%s); falling back to the local table"
          % _exc, file=sys.stderr)
    galaxy_places = None
    _GAZETTEER = False

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES_PATH = os.path.join(HERE, "sources_slavery.json")
OUT_PATH = os.path.join(HERE, "wire_slavery.json")

RETAIN_DAYS = 45
MAX_ITEMS = 1200
WORKERS = 10
NOTABLE_SCORE = 3       # at or above this a story is marked as well documented

# --------------------------------------------------------------------------
# Plumbing: fetching, feed parsing, word-edge matching, fingerprints.
# --------------------------------------------------------------------------
USER_AGENT = ("Mozilla/5.0 (compatible; anti-slavery-feed/1.0; "
              "+https://github.com/WelcomeToYourGalaxy/anti-slavery-feed)")

TIMEOUT = 25

SNIPPET_CHARS = 240

TAG_RE = re.compile(r"<[^>]+>")

WS_RE = re.compile(r"\s+")

PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def build_gnews_url(loc):
    # the wire keeps 45 days, so ask the search for the same span
    q = loc["query"] + " when:45d"
    return ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q) +
            "&hl=" + loc["hl"] + "&gl=" + loc["gl"] + "&ceid=" + loc["ceid"])


READ_BUDGET_MIN = 40          # minutes spent reading wires

# The wall-clock budget for reading wires. Past it the remaining sources are
# recorded unreachable and the harvest finishes on what it has, because the
# wire is only written at the end of run() and a job killed by the workflow
# timeout commits nothing at all.
DEADLINE = None


def out_of_time():
    return DEADLINE is not None and time.monotonic() > DEADLINE


def fetch(url, tries=3):
    last = None
    for attempt in range(tries):
        if out_of_time():
            return None
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
                "Accept-Encoding": "gzip",
                "Accept-Language": "*",
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                return raw
        except urllib.error.HTTPError as exc:
            last = exc
            # Being rate-limited or refused is an answer. Asking twice more
            # spends a worker slot to be told the same thing.
            if exc.code in (403, 429, 451):
                time.sleep(1.5)
                break
            time.sleep(1.5 * (attempt + 1))
        except Exception as exc:                       # noqa: BLE001 — report, don't crash the run
            last = exc
            time.sleep(1.5 * (attempt + 1))
    print("  ! unreachable: %s (%s)" % (url[:90], last), file=sys.stderr)
    return None


def strip_ns(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def text_of(el):
    return WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", el.text or ""))).strip() if el is not None else ""


def child(node, *names):
    for kid in node:
        if strip_ns(kid.tag) in names:
            return kid
    return None


def parse_date(raw):
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:  # noqa: BLE001
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return None


def split_outlet(title, outlet, src_name, link=""):
    """Google News appends ' - Outlet' to every headline. Splitting it off is
    the only change ever made to a title, and the outlet is kept beside it."""
    if outlet and title.endswith(" - " + outlet):
        return title[: -(len(outlet) + 3)].strip(), outlet
    if not outlet and (src_name.startswith("Google News") or src_name.startswith("Search")
                       or "news.google.com" in (link or "")) and " - " in title:
        head, _, tail = title.rpartition(" - ")
        if head and 2 <= len(tail) <= 45:
            return head.strip(), tail.strip()
    return title, outlet


def parse_feed(raw, src):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        # Some publishers serve a stray byte before the declaration.
        try:
            root = ET.fromstring(raw[raw.index(b"<"):])
        except Exception:  # noqa: BLE001
            return []

    nodes = [n for n in root.iter() if strip_ns(n.tag) == "item"]
    atom = False
    if not nodes:
        nodes = [n for n in root.iter() if strip_ns(n.tag) == "entry"]
        atom = True

    out = []
    for n in nodes:
        title = text_of(child(n, "title"))
        if atom:
            link = ""
            for kid in n:
                if strip_ns(kid.tag) == "link" and kid.get("rel", "alternate") == "alternate":
                    link = kid.get("href", "")
                    break
        else:
            link_el = child(n, "link")
            link = (link_el.text or "").strip() if link_el is not None else ""
            if not link:
                link = text_of(child(n, "guid"))
        if not title or not link:
            continue

        outlet_el = child(n, "source")
        outlet = text_of(outlet_el) if outlet_el is not None else ""
        title, outlet = split_outlet(title, outlet, src["name"], link)

        stamp = parse_date(text_of(child(n, "pubDate", "published", "updated", "date")))
        snippet = text_of(child(n, "description", "summary", "content"))[:SNIPPET_CHARS]

        out.append({
            "t": title,
            "u": link,
            "o": outlet or re.sub(r"^(Google News|Search) · ", "", src["name"]),
            "g": src["lang"],
            "r": src["region"],
            "k": src.get("kind", "news"),
            "d": stamp,
            "s": snippet,
            "w": src["name"],
        })
    return out


def _compile(term):
    if any(ord(ch) > 0x24F for ch in term):        # non-Latin script
        # substring matching is already prefix-like in scripts without word
        # breaks, so a trailing * is dropped rather than searched for.
        return term[:-1] if term.endswith("*") else term
    if term.endswith("*"):
        return re.compile(r"(?<![a-z0-9])" + re.escape(term[:-1]) + r"[a-z0-9\-]*", re.I)
    return re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", re.I)


def _compile_all(terms):
    return [_compile(t) for t in terms]


def hit(text, compiled):
    """True when any compiled term matches."""
    for c in compiled:
        if isinstance(c, str):
            if c in text:
                return True
        elif c.search(text):
            return True
    return False


def fingerprint(title):
    norm = PUNCT_RE.sub(" ", title.lower())
    return " ".join(WS_RE.sub(" ", norm).strip().split()[:9])


def canon_url(url):
    try:
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parts.query)
        query = [(k, v) for k, v in query if not k.lower().startswith("utm_")]
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"),
                                        urllib.parse.urlencode(query), ""))
    except Exception:  # noqa: BLE001
        return url


# --------------------------------------------------------------------------
# The gate.
#
# ON   — the story is about forced labour, modern slavery, trafficking, child
#        labour, debt bondage or servitude. Carried from harvest_wire.py's ON
#        list and extended to every language the wire queries in. A term with
#        guards counts only when one of its guards is also present.
# Rule — a subject term in the HEADLINE, or two different subject terms
#        anywhere in headline and snippet. One term buried in a snippet is how
#        a crime round-up that mentions trafficking in its last line gets in;
#        harvest_wire.py and the map both use this rule, and so does this.
# OFF  — refused even when ON matches: the idioms ("slave to fashion", "wage
#        slave"), the historical institution ("transatlantic slave trade"), UK
#        party politics, childbirth, childcare policy, and noise. The union of
#        harvest_wire.py's OFF, the map's WIRE_OFF and its WIRE_STOP, with the
#        few changes listed beside OFF.
# --------------------------------------------------------------------------
_PEOPLE_GUARD = ["worker*", "women", "woman", "girls", "girl", "boys", "boy", "children", "child",
                 "victims", "victim", "people", "migrants", "migrant*", "persons", "men",
                 "labour", "labor", "sex", "fishermen", "fishers", "crew*", "into", "to work",
                 "teen*", "minors", "domestic"]

ON = [
    # --- English, from harvest_wire.py
    ("forced labour", None), ("forced labor", None), ("modern slavery", None),
    ("human trafficking", None), ("labour trafficking", None), ("labor trafficking", None),
    ("trafficked", _PEOPLE_GUARD + ["trafficked for", "trafficked to", "trafficked from", "trafficked into"]), ("trafficking ring*", None),
    ("child labour", None), ("child labor", None), ("children working", None),
    ("underage worker*", None), ("debt bondage", None), ("bonded labour", None),
    ("bonded labor", None), ("indentured", None), ("servitude", None),
    ("slave labour", None), ("slave labor", None), ("recruitment fee*", None),
    ("labour broker*", None), ("labor broker*", None), ("gangmaster*", None),
    ("passport confiscat*", None), ("withheld passport*", None), ("wage theft", None),
    ("unpaid wages", None), ("kafala", None), ("sponsorship system", ["kafala", "migrant*", "worker*", "gulf"]),
    ("withhold release order*", None), ("uflpa", None), ("forced labour regulation", None),
    ("domestic servitude", None), ("forced marriage", None), ("worst forms of child labour", None),
    # --- English, from the map's own signal list (index.html) and its queries
    ("forced labourer*", None), ("forced laborer*", None), ("forced to work", None),
    ("modern-day slavery", None), ("slavery-like", None), ("slave-like", None),
    ("conditions akin to slavery", None), ("human trafficker*", None), ("sex trafficking", None),
    ("child trafficking", None), ("trafficking in persons", None), ("trafficking victim*", None),
    ("victims of trafficking", None), ("trafficking survivor*", None), ("survivor of trafficking", None),
    ("child worker*", None), ("bonded labourer*", None), ("bonded laborer*", None),
    ("confiscated passport*", None), ("labour exploitation", None), ("labor exploitation", None),
    ("exploitation of workers", None), ("forced labor regulation", None),
    ("scam compound*", ["traffick*", "forced", "held", "rescued", "worker*", "victims", "freed"]),
    ("scam centre*", ["traffick*", "forced", "held", "rescued", "worker*", "victims", "freed"]),
    ("scam center*", ["traffick*", "forced", "held", "rescued", "worker*", "victims", "freed"]),
    ("enslaved", ["worker*", "migrant*", "women", "children", "fishermen", "men", "held", "rescued", "freed"]),
    ("slavery", ["rescued", "freed", "raid*", "arrest*", "charged", "convicted", "sentenced",
                 "prosecut*", "police", "workers"]),
    # --- Spanish
    ("trabajo forzoso", None), ("trabajo forzado", None), ("trabajos forzados", None),
    ("trata de personas", None), ("trata de seres humanos", None), ("trata laboral", None),
    ("trabajo infantil", None), ("esclavitud moderna", None), ("esclavitud laboral", None),
    ("servidumbre", None), ("explotación laboral", None), ("trabajo esclavo", None),
    ("condiciones de esclavitud", None), ("semiesclavitud", None),
    # --- Portuguese
    ("trabalho forçado", None), ("trabalho escravo", None), ("análoga à escravidão", None),
    ("análogas à escravidão", None), ("análoga a escravidão", None), ("análogo à escravidão", None),
    ("condições análogas", None), ("tráfico de pessoas", None), ("tráfico humano", None),
    ("trabalho infantil", None), ("escravidão moderna", None), ("escravidão contemporânea", None),
    ("servidão por dívida", None),
    ("escravidão", ["trabalho", "trabalhador*", "resgat*", "condições", "contemporânea", "moderna"]),
    # --- French
    ("travail forcé", None), ("traite des êtres humains", None), ("traite d'êtres humains", None),
    ("travail des enfants", None), ("esclavage moderne", None), ("esclavage contemporain", None),
    ("exploitation par le travail", None), ("servitude pour dettes", None),
    # --- German
    ("zwangsarbeit*", None), ("menschenhandel*", None), ("kinderarbeit*", None),
    ("moderne sklaverei", None), ("arbeitsausbeutung", None), ("schuldknechtschaft", None),
    # --- Italian
    ("lavoro forzato", None), ("tratta di esseri umani", None), ("tratta degli esseri umani", None),
    ("lavoro minorile", None), ("caporalato", None), ("sfruttamento lavorativo", None),
    ("sfruttamento del lavoro", None), ("schiavitù moderna", None), ("riduzione in schiavitù", None),
    ("intermediazione illecita", None),
    # --- Dutch, Swedish, Polish, Greek
    ("dwangarbeid", None), ("mensenhandel", None), ("kinderarbeid", None),
    ("moderne slavernij", None), ("arbeidsuitbuiting", None),
    ("tvångsarbete", None), ("människohandel", None), ("barnarbete", None), ("modernt slaveri", None),
    ("praca przymusowa", None), ("pracy przymusowej", None), ("handel ludźmi", None),
    ("handlu ludźmi", None), ("praca dzieci", None), ("pracy dzieci", None),
    ("współczesne niewolnictwo", None),
    ("καταναγκαστική εργασία", None), ("καταναγκαστικής εργασίας", None),
    ("εμπορία ανθρώπων", None), ("εμπορίας ανθρώπων", None), ("παιδική εργασία", None),
    ("παιδικής εργασίας", None),
    # --- Russian, Ukrainian, Turkish
    ("принудительный труд", None), ("принудительного труда", None), ("принудительным трудом", None),
    ("торговля людьми", None), ("торговли людьми", None), ("торговлей людьми", None),
    ("детский труд", None), ("детского труда", None), ("трудовое рабство", None),
    ("трудового рабства", None), ("современное рабство", None), ("современного рабства", None),
    ("примусова праця", None), ("примусової праці", None), ("торгівля людьми", None),
    ("торгівлі людьми", None), ("дитяча праця", None), ("дитячої праці", None),
    ("zorla çalıştırma", None), ("zorla çalıştırıl*", None), ("insan ticareti", None),
    ("çocuk işçi*", None), ("modern kölelik", None), ("zorunlu çalışma", None),
    # --- Arabic, Persian
    ("العمل القسري", None), ("العمل الجبري", None), ("الاتجار بالبشر", None),
    ("الاتجار بالأشخاص", None), ("عمالة الأطفال", None), ("عمل الأطفال", None),
    ("العبودية الحديثة", None), ("نظام الكفالة", None),
    ("کار اجباری", None), ("قاچاق انسان", None), ("کار کودکان", None), ("بردگی مدرن", None),
    # --- Hindi, Bengali
    ("बंधुआ मजदूर", None), ("बंधुआ मज़दूर", None), ("मानव तस्करी", None), ("बाल श्रम", None),
    ("जबरन मजदूरी", None), ("बाल मजदूर", None), ("बाल मज़दूर", None),
    ("মানব পাচার", None), ("শিশুশ্রম", None), ("শিশু শ্রম", None), ("জোরপূর্বক শ্রম", None),
    ("বাধ্যতামূলক শ্রম", None),
    # --- Indonesian, Vietnamese, Thai
    ("kerja paksa", None), ("perdagangan orang", None), ("perdagangan manusia", None),
    ("pekerja anak", None), ("perbudakan modern", None), ("tppo", None), ("perbudakan", None),
    ("lao động cưỡng bức", None), ("mua bán người", None), ("buôn bán người", None),
    ("lao động trẻ em", None),
    ("แรงงานบังคับ", None), ("การค้ามนุษย์", None), ("ค้ามนุษย์", None), ("แรงงานเด็ก", None),
    # --- Japanese, Chinese, Korean
    ("強制労働", None), ("人身売買", None), ("人身取引", None), ("児童労働", None), ("現代奴隷", None),
    ("强迫劳动", None), ("強迫勞動", None), ("强制劳动", None), ("強制勞動", None),
    ("人口贩卖", None), ("人口販賣", None), ("人口販運", None), ("贩卖人口", None), ("童工", None),
    ("现代奴隶", None), ("現代奴隸", None), ("强迫劳工", None), ("強迫勞工", None),
    ("강제노동", None), ("강제 노동", None), ("인신매매", None), ("아동노동", None), ("아동 노동", None),
    # --- Swahili
    ("kazi ya kulazimishwa", None), ("kazi za kulazimishwa", None), ("biashara ya binadamu", None),
    ("biashara haramu ya binadamu", None), ("usafirishaji haramu wa binadamu", None),
    ("ajira kwa watoto", None), ("utumwa wa kisasa", None),
]

OFF = [
    # --- metaphor (harvest_wire.py, WIRE_OFF, WIRE_STOP)
    "wage slave*", "slave to fashion", "slave to the", "slave to my", "slaves to the algorithm",
    "slaves to fashion", "slaves to the", "fashion slave", "slave away", "slave labour of love",
    "slave to the rhythm", "modern slavery to the algorithm",
    # --- the historical institution, which has its own literature and is not this
    "transatlantic slave*", "slave trade history", "slave trade abolition",
    "abolition of slavery anniversary", "slave ship wreck", "plantation museum", "plantation tour",
    "slavery museum", "slave narrative", "slave rebellion of 17*", "slave rebellion of 18*",
    "reparations for slavery", "confederate", "american civil war", "civil war era",
    # --- the same, for the forced labour of the Nazi period, which floods German queries
    "ns-zwangsarbeit", "ns-zeit", "nazi-zeit", "gedenkstätte", "stolperstein*",
    # --- UK party politics
    "labour party", "labor party", "labour leader", "labour mp", "labour mps", "labour government",
    "labour manifesto", "labour councillor", "shadow cabinet", "keir starmer",
    # --- childbirth
    "went into labour", "labour ward", "induced labour", "labour pains", "in labour with",
    "labour and delivery", "childbirth",
    # --- childcare policy and children's culture
    "child benefit", "childcare funding", "child tax credit", "child care costs", "child actor*",
    "child prodigy", "children's book*", "children's film*", "school holidays",
    # --- noise
    "horoscope", "box office", "recipe", "match report", "transfer window", "transfer fee",
    "premier league", "celebrity", "royal wedding", "royal family", "album", "album review",
    "concert tour", "video game", "share price", "shares rise", "shares fall", "stock market",
    "stock tips", "earnings beat", "quarterly earnings", "quarterly results", "webinar",
    "register now", "register here", "join us", "save the date", "newsletter signup",
    "subscribe today", "apply now", "job opening", "we are hiring", "podcast episode",
    "book review", "book launch", "call for papers", "gift guide", "black friday", "betting odds",
    "film festival", "red carpet", "fashion week", "obituary", "weather forecast",
    "sponsored content", "press release distribution",
]
# Changes from the map's lists, each because the word refused stories this wire
# exists to carry:
#   "crypto", "bitcoin", "nft"  — scam-compound trafficking is reported through them
#   "football", "soccer", "sports" — World Cup construction workers, child players trafficked
#   "royal" → "royal wedding", "royal family" — Royal Thai/Malaysia Police, royal commissions
#   "civil war" → "american civil war", "civil war era" — Sudan, Myanmar, Yemen today
#   "slaves to", "a slave to" → their idioms only — "sold as slaves to traffickers"
#   "international" dropped from WIRE_GLOBAL below — Amnesty International, ILO

LANG_NAMES = {
    "en": "English", "es": "Español", "pt": "Português", "fr": "Français", "de": "Deutsch",
    "it": "Italiano", "nl": "Nederlands", "sv": "Svenska", "pl": "Polski", "el": "Ελληνικά",
    "ru": "Русский", "uk": "Українська", "tr": "Türkçe", "ar": "العربية", "fa": "فارسی",
    "hi": "हिन्दी", "bn": "বাংলা", "id": "Bahasa Indonesia", "vi": "Tiếng Việt", "th": "ไทย",
    "ja": "日本語", "zh": "中文", "ko": "한국어", "sw": "Kiswahili",
}

# --------------------------------------------------------------------------
# Subjects. The eleven threads of the map's WIRE_THREADS, with the same ids and
# terms, plus the same subjects in the other languages the wire reads. The
# generic subject words ("forced labour", "slavery", "trafficking") are left out
# of the enforcement thread here: every item passed the gate on them, so in a
# multi-tag feed they would file everything under enforcement.
#
# A story that no thread claims goes to "Other reports", as it does in the
# map's wire panel. It is an honest bucket, not a guess.
# --------------------------------------------------------------------------
TOPICS = [
    ("enforcement", "Raids, prosecutions & enforcement", [
        "prosecut*", "convicted", "conviction*", "sentenced", "raid*", "rescued", "freed",
        "arrest*", "labour inspection*", "labor inspection*", "labour inspector*",
        "labor inspector*", "inspectorate", "penalt*", "fined", "fines", "charged", "indict*",
        "sting", "crackdown", "busted", "dismantled", "jailed", "guilty", "police operation",
        # it
        "arrestat*", "arresto", "arresti", "denunciat*", "condannat*", "condanna", "sequestr*",
        "blitz", "controlli", "ispettorato", "ispettori", "carabinieri", "guardia di finanza",
        "indagat*", "misure cautelari", "liberat*",
        # es
        "detenid*", "detención", "rescatad*", "liberad*", "condenad*", "desarticul*",
        "operativo", "allanamiento*", "imputad*", "inspección de trabajo",
        # pt
        "resgatad*", "resgate", "presos", "prisão", "operação", "fiscalização",
        "auditores-fiscais", "auditores fiscais", "libertad*", "flagrante", "lista suja",
        # fr
        "interpellé*", "arrêté*", "condamné*", "démantel*", "libéré*", "inspection du travail",
        "mis en examen", "garde à vue", "perquisition*",
        # de, nl
        "festgenommen", "festnahme*", "verurteilt", "razzia", "razzien", "durchsuch*",
        "anklage", "befreit", "ermittl*", "gearresteerd", "aangehouden", "veroordeeld", "inval",
        # other scripts and languages
        "逮捕", "判决", "判決", "摘発", "起訴", "救出", "арест", "задержан", "осужден", "приговор",
        "освобожден", "ditangkap", "dibebaskan", "diselamatkan", "divonis", "จับกุม", "체포",
        "구출", "유죄", "gözaltı*", "tutuklan*", "اعتقال", "القبض", "ضبط", "गिरफ्तार", "छापे",
        "छापा", "গ্রেপ্তার", "উদ্ধার",
    ]),
    ("child", "Child labour", [
        "child labour", "child labor", "child worker*", "children working", "underage worker*",
        "underage", "minor employed", "minors employed", "school dropout*", "hazardous work",
        "worst forms", "child mining", "child miner*", "child domestic", "out of school",
        "birth registration", "child trafficking", "child soldier*",
        "trabajo infantil", "niños trabajando", "trabalho infantil", "travail des enfants",
        "enfants travailleurs", "kinderarbeit", "lavoro minorile", "minori sfruttati",
        "kinderarbeid", "barnarbete", "praca dzieci", "pracy dzieci", "детский труд",
        "детского труда", "дитяча праця", "дитячої праці", "çocuk işçi*", "عمالة الأطفال",
        "عمل الأطفال", "बाल श्रम", "बाल मजदूर", "बाल मज़दूर", "শিশুশ্রম", "শিশু শ্রম",
        "pekerja anak", "lao động trẻ em", "แรงงานเด็ก", "児童労働", "童工", "아동노동",
        "아동 노동", "ajira kwa watoto", "παιδική εργασία", "παιδικής εργασίας", "کار کودکان",
    ]),
    ("supply", "Supply chains & sourcing", [
        "supply chain*", "supplier*", "sourcing", "due diligence", "audit", "audits", "audited",
        "auditor", "auditors", "auditing", "traceab*",
        "tier 1", "tier 2", "subcontract*", "sweatshop*", "factory", "factories", "garment*",
        "apparel", "brand", "brands", "branded", "procurement code", "code of conduct", "csddd", "lksg",
        "modern slavery statement*", "cocoa", "cotton", "palm oil", "seafood", "shrimp",
        "solar panel*", "polysilicon", "retailer*",
        "cadena de suministro", "cadenas de suministro", "cadeia de suprimentos",
        "cadeia produtiva", "chaîne d'approvisionnement", "chaînes d'approvisionnement",
        "devoir de vigilance", "lieferkette*", "lieferkettengesetz", "sorgfaltspflicht*",
        "filiera", "filiere", "catena di fornitura", "供应链", "供應鏈", "サプライチェーン",
        "공급망", "цепочк",
    ]),
    ("imports", "Import bans, customs & trade", [
        "import ban*", "withhold release order*", "wro", "wros", "uflpa", "detained shipment*",
        "customs seiz*", "forced labour regulation", "forced labor regulation",
        "trade preference*", "gsp", "tariff*", "sanction*", "entity list", "blacklist*",
        "banned from import*", "port of entry", "cbp", "customs and border protection",
        "imports", "import", "goods made with", "products made with", "made with forced labour",
        "made with forced labor", "forced labour bans", "forced labor bans", "block goods",
        "mercancías", "mercadorias", "zwangsarbeitsverordnung", "importverbot", "einfuhrverbot", "zoll", "prohibición de importación", "aduana*",
        "interdiction d'importation", "douane*", "divieto di importazione", "dogana", "dogane",
        "alfândega", "proibição de importação", "进口禁令", "輸入禁止", "수입 금지",
    ]),
    ("trafficking", "Trafficking & recruitment", [
        "human trafficking", "traffick*", "smuggl*", "recruitment fee*", "recruiter*",
        "labour broker*", "labor broker*", "recruitment agenc*", "debt bondage",
        "bonded labour", "bonded labor", "passport confiscat*", "confiscated passport*",
        "withheld passport*", "contract substitution", "kafala", "sponsorship system",
        "deceptive recruitment", "scam centre*", "scam center*", "scam compound*", "gangmaster*",
        "caporalato", "caporal*", "intermediazione illecita", "sfruttamento lavorativo",
        "trata de personas", "trata de seres humanos", "tráfico de pessoas", "tráfico humano",
        "aliciamento", "traite des êtres humains", "traite d'êtres humains", "menschenhandel",
        "schleuser*", "tratta di esseri umani", "tratta degli esseri umani", "mensenhandel", "människohandel", "handel ludźmi",
        "handlu ludźmi", "торговля людьми", "торговли людьми", "торговлей людьми",
        "торгівля людьми", "торгівлі людьми", "insan ticareti", "الاتجار بالبشر",
        "الاتجار بالأشخاص", "قاچاق انسان", "मानव तस्करी", "মানব পাচার", "perdagangan orang",
        "perdagangan manusia", "tppo", "mua bán người", "buôn bán người", "การค้ามนุษย์",
        "ค้ามนุษย์", "人身売買", "人身取引", "人口贩卖", "人口販賣", "人口販運", "贩卖人口",
        "인신매매", "biashara ya binadamu", "usafirishaji haramu wa binadamu", "εμπορία ανθρώπων",
        "servidumbre por deudas", "servidão por dívida", "servitude pour dettes",
        "schuldknechtschaft", "बंधुआ मजदूर", "बंधुआ मज़दूर",
    ]),
    ("migrant", "Migrant & contract workers", [
        "migrant worker*", "guest worker*", "seasonal worker*", "work visa*", "h-2a", "h-2b",
        "undocumented worker*", "deport*", "labour migration", "labor migration",
        "remittance*", "wage theft", "unpaid wages", "dormitor*", "labour camp*", "labor camp*",
        "employer-tied visa*", "domestic worker*", "farmworker*", "farm worker*", "migrant*",
        "braccianti", "bracciante", "lavoratori stranieri", "migranti", "stagionali",
        "jornaleros", "jornalero*", "temporeros", "trabajadores migrantes", "migrantes",
        "trabajadoras del hogar", "trabalhadores migrantes", "imigrantes",
        "travailleurs migrants", "saisonniers", "travailleurs sans papiers", "saisonarbeit*",
        "erntehelfer*", "wanderarbeit*", "arbeitsmigrant*", "migranten",
        "外国人労働者", "技能実習", "移工", "外籍勞工", "农民工", "이주노동자", "мигрант",
        "göçmen işçi*", "العمال المهاجرين", "عمال مهاجرين", "العمالة الوافدة", "प्रवासी मजदूर",
        "প্রবাসী শ্রমিক", "pekerja migran", "lao động di cư", "แรงงานข้ามชาติ",
        "แรงงานต่างด้าว", "wafanyakazi wa ndani",
    ]),
    ("sea", "Fishing, seafarers & work at sea", [
        "fishing vessel*", "distant-water", "distant water", "iuu fishing", "seafarer*",
        "crew abandon*", "abandoned crew*", "transshipment", "transhipment", "at sea for",
        "flag of convenience", "flags of convenience", "squid fleet", "fishing fleet*",
        "port state control", "maritime labour convention", "fishing boat*", "fishermen",
        "fishers", "trawler*", "fishing crew*",
        "buque pesquero", "barco pesquero", "pesquero*", "pescadores", "navio pesqueiro",
        "pêche", "marins", "pêcheurs", "fischerei", "fischkutter", "seeleute", "peschereccio",
        "pescatori", "marittimi", "远洋渔业", "渔船", "漁船", "kapal ikan", "anak buah kapal",
        "เรือประมง", "선원", "원양어선",
    ]),
    ("mining", "Mining, minerals & extraction", [
        "artisanal mining", "cobalt", "mica mining", "mica mine*", "mica industry",
        "mica sector", "mica supply", "mica belt", "mica pickers", "gold mining", "gold mine*", "coltan", "tantalum",
        "conflict mineral*", "quarr*", "brick kiln*", "kiln*", "stone crush*", "charcoal",
        "mine collapse", "child miner*", "critical mineral*", "garimpo", "garimpeiro*",
        "carvoaria*", "minería", "mineração", "cobalto", "coltán", "orpaillage", "mine d'or",
        "bergbau", "kobalt", "miniera", "ladrillera*", "olaria*", "ईंट भट्ठ", "ইটভাটা", "钴",
        "コバルト", "tambang",
    ]),
    ("state", "State-imposed & prison labour", [
        "state-imposed", "prison labour", "prison labor", "forced labour transfer*",
        "labour transfer*", "labor transfer*", "re-education", "internment", "conscript*",
        "national service", "compulsory labour", "compulsory labor", "xinjiang", "uyghur*",
        "uighur*", "penal labour", "forced conscription", "convict leasing", "inmate labor",
        "inmate labour", "north korean workers", "trabajo penitenciario", "trabalho prisional",
        "travail pénitentiaire", "ouïghour*", "ouïgour*", "uigur*", "uiguren",
        "gefängnisarbeit", "新疆", "维吾尔", "維吾爾", "ウイグル", "위구르", "劳改",
    ]),
    ("law", "Laws, duties & courts", [
        "due diligence law", "supply chain act", "modern slavery act", "forced labour ban",
        "forced labor ban", "ratif*", "convention 29", "convention 182", "ilo committee",
        "lawsuit*", "class action", "court ruled", "court", "courts", "tribunal*", "appeal*",
        "legislation", "bill passed", "regulation adopt*", "national contact point",
        "directive", "law", "laws", "proyecto de ley", "projeto de lei", "verordnung", "trt", "tst", "judge*", "ruling", "verdict", "supreme court", "high court",
        "legge", "sentenza", "tribunale", "cassazione", "processo", "decreto", "ley",
        "sentencia", "juzgado", "juicio", "lei", "sentença", "justiça do trabalho",
        "ação civil", "loi", "procès", "jugement", "cour", "gesetz*", "gericht*", "urteil*",
        "klage", "prozess", "richtlinie", "rechtbank", "法院", "法律", "裁判", "法案",
        "법원", "법안", "закон", "mahkeme", "yasa", "محكمة", "قانون", "अदालत", "कानून",
        "pengadilan", "undang-undang", "mahakama", "sheria",
    ]),
    ("survivors", "Survivors, remedy & compensation", [
        "survivor*", "compensation", "restitution", "back wages", "back pay", "settlement*",
        "remedy", "remediation", "victim support", "shelter*", "reintegration",
        "rehabilitation", "damages awarded", "damages", "recovered wages",
        "victim identification", "residence permit*", "non-punishment", "hotline",
        "repatriat*", "víctima*", "vítima*", "victime*", "vittim*", "opfer", "slachtoffer*",
        "indemniz*", "risarcimento", "risarcit*", "indennizzo", "entschädigung",
        "indemnisation", "reparación", "reparação", "acolhida", "abrigo",
        "permesso di soggiorno", "被害者", "受害者", "피해자", "жертв", "korban",
    ]),
]
OTHER_TOPIC = ("other", "Other reports")

# --------------------------------------------------------------------------
# Direction. Harm reported, or action taken. Most stories are one or the other,
# some are both — a raid that freed workers is action taken against harm the
# story also describes — and the difference is the first thing a reader wants.
# A story carrying neither is filed as harm reported: it describes the
# situation without reporting anything done about it.
# --------------------------------------------------------------------------
HARM = [
    "exploit*", "trafficked", "forced to work", "abuse*", "abused", "died", "death*", "killed",
    "beaten", "confined", "locked", "held captive", "confiscat*", "withheld", "unpaid", "debt",
    "bonded", "enslaved", "abandoned", "allegation*", "alleged*", "accused", "linked to",
    "report finds", "investigation finds", "reveals", "exposed", "uncovered", "warn*",
    "at risk", "rising", "surge", "victims", "held against",
    "sfruttat*", "sfruttamento", "vittim*", "morto", "morti", "maltrattat*", "schiavi", "ricatt*",
    "explotad*", "explotación", "víctima*", "abusos", "esclavizad*", "murió", "muertos",
    "explorad*", "exploração", "vítima*", "escravizad*", "condições degradantes",
    "jornada exaustiva", "maus-tratos", "exploité*", "exploitation", "victime*", "maltrait*",
    "ausgebeutet", "ausbeutung", "misshandelt", "missstände",
    "剥削", "搾取", "착취", "эксплуатац", "sömürü*", "استغلال", "शोषण", "শোষণ", "eksploitasi",
    "bóc lột",
]
ACTION = [
    "rescued", "freed", "raid*", "arrested", "arrests", "charged", "convicted", "sentenced",
    "prosecuted", "prosecution*", "indicted", "fined", "banned", "ban", "bans", "blocked",
    "seized", "detained", "withhold release order*", "entity list", "sanctioned", "sanctions",
    "compensation", "compensated", "back wages", "settlement", "restitution", "ruling",
    "ruled", "injunction", "ratified", "passed", "adopted", "enacted", "reform*", "dismantled",
    "busted", "crackdown", "jailed", "guilty", "ordered to pay", "repaid", "reimbursed", "won the",
    "won a", "won back", "won compensation", "wins", "victory", "closed down", "shut down", "suspended", "revoked",
    "arrestat*", "arresto", "arresti", "denunciat*", "condannat*", "condanna", "sequestr*",
    "blitz", "liberat*", "salvat*", "misure cautelari", "sentenza", "risarcit*",
    "detenid*", "rescatad*", "liberad*", "condenad*", "desarticul*", "sentencia", "multa*",
    "sancion*", "prohib*", "resgatad*", "resgate", "presos", "prisão", "operação",
    "libertad*", "multad*", "indenizaç*", "lista suja", "interpellé*", "condamné*",
    "démantel*", "libéré*", "interdit*", "amende*", "festgenommen", "verurteilt", "razzia",
    "befreit", "verboten", "verbot", "bußgeld", "entschädigung",
    "逮捕", "判决", "判決", "摘発", "起訴", "救出", "禁止", "арест", "задержан", "осужден",
    "приговор", "освобожден", "ditangkap", "dibebaskan", "diselamatkan", "divonis", "จับกุม",
    "체포", "구출", "유죄", "gözaltı*", "tutuklan*", "اعتقال", "القبض", "ضبط", "गिरफ्तार",
    "छापा", "छापे", "গ্রেপ্তার", "উদ্ধার",
]

# --------------------------------------------------------------------------
# Evidence signals for the score.
# --------------------------------------------------------------------------
DOCUMENTED = [
    "ruling", "ruled", "verdict", "judgment", "judgement", "convicted", "sentenced", "charged",
    "indicted", "arrested", "rescued", "freed", "seized", "raid", "raided", "fined", "banned",
    "withhold release order*", "detained shipment*", "compensation awarded", "settlement reached",
    "ordered to pay", "guilty", "jailed",
    "condannat*", "arrestat*", "sequestr*", "sentenza", "condenad*", "detenid*", "rescatad*",
    "resgatad*", "presos", "sentencia", "condamné*", "interpellé*", "verurteilt", "festgenommen",
    "befreit", "逮捕", "判决", "判決", "摘発", "арест", "осужден", "ditangkap",
]
INSTITUTIONAL = [
    "united nations", "ilo", "international labour organization", "international labor organization",
    "unicef", "iom", "unodc", "ohchr", "special rapporteur", "walk free", "global slavery index",
    "trafficking in persons report", "tip report", "department of labor", "cbp",
    "customs and border protection", "homeland security", "european commission",
    "european parliament", "supreme court", "high court", "court of appeal",
    "human rights watch", "amnesty international", "anti-slavery international",
    "business & human rights resource centre", "business and human rights resource centre",
    "oecd", "national contact point", "report finds", "study finds", "survey finds",
    "investigation", "court documents", "court records", "government data", "official figures",
    "labour inspectorate", "labor inspectorate", "ministério público do trabalho", "mpt",
    "lista suja", "auditores-fiscais", "auditores fiscais", "ispettorato", "guardia di finanza",
    "carabinieri", "fiscalía", "ministerio de trabajo", "inspección de trabajo", "oit", "oim",
    "onu", "ministério do trabalho", "inspection du travail",
]
MEASURED = [
    "per cent", "percent", "million", "millions of", "thousands of", "hundreds of",
    "dozens of", "billion",
]
# a share or a sum: "40%", "$2.3m", "€ 500"
MEASURED_SIGN_RE = re.compile(r"\d\s?%|[$€£]\s?\d")
# a number standing in front of the people it counts: "43 workers", "1,200 victims"
MEASURED_RE = re.compile(
    r"\b\d[\d,.]*\s+(?:\w+\s+)?(?:workers?|people|victims?|children|migrants?|women|men|"
    r"persons?|labou?rers?|fishermen|crew|employees|minors|girls|boys|families|"
    r"lavoratori|braccianti|persone|trabajadores|personas|trabalhadores|pessoas|"
    r"personnes|travailleurs|arbeiter|menschen|opfer|vittime|víctimas|vítimas)\b", re.I)
PROJECTED = [
    "expected to", "set to", "due to", "will take effect", "comes into force", "come into force",
    "enter into force", "enters into force", "from 2027", "by 2027", "by 2030", "proposed",
    "draft law", "draft bill", "consultation", "deadline", "hearing scheduled", "under review",
    "next year", "in the coming months", "to be voted", "pending",
]

# --------------------------------------------------------------------------
# Several countries at once. WIRE_GLOBAL from the map, used the same way: a
# story carrying one of these, or naming two or more countries, is also filed
# under "International / several countries". "international" on its own is
# dropped — it is the last word of Amnesty International, Anti-Slavery
# International and the International Labour Organization — and kept in its
# looser phrases instead.
# --------------------------------------------------------------------------
WIRE_GLOBAL = [
    "e.u.", "eu's", "eu rules", "eu law", "eu ban", "eu regulation", "eu-wide", "international law", "international standards", "international community",
    "internationally", "worldwide", "global", "globally", "cross-border", "transnational",
    "treaty", "united nations", "u.n.", "across europe", "across africa", "across asia",
    "across the world", "around the world", "multiple countries", "dozens of countries",
    "several countries", "many countries", "world bank", "eu-wide", "europe-wide", "oecd",
    "g20", "g7", "council of europe", "human rights court",
]

# Geographic features that carry a country name or demonym without being about
# the country, removed before placement (from harvest_wire.py's tag_geo).
GEO_STRIP = [
    "indian ocean", "south china sea", "east china sea", "persian gulf", "arabian sea",
    "korean peninsula", "english channel", "irish sea", "north american", "south american",
    "latin american", "african union", "asian development bank", "native american",
    "american indian", "latin america", "south america", "central america", "north america",
    "américa latina", "america latina", "amérique latine", "lateinamerika",
]

# --------------------------------------------------------------------------
# Where the story is, in three levels: region, subregion, country. A story
# naming Qatar files under Qatar, the Gulf states and the Middle East at once.
# Names in the main languages the wire reads, and demonyms, because headlines
# say "Indian workers" or "Brazilian prosecutors" far more often than they name
# the country. The shared gazetteer fills in whatever these do not catch: any
# city, province or country in any of its languages resolves to an ISO code,
# and the code files the story here.
# --------------------------------------------------------------------------
GEO3 = [
 ("africa", "Africa", [
   ("af-w", "West Africa", [
     ("ng", "Nigeria", ["nigeria", "nigerian*", "lagos", "abuja", "kano", "edo state", "benin city"]),
     ("gh", "Ghana", ["ghana", "ghanaian*", "accra"]),
     ("ci", "Côte d'Ivoire", ["côte d'ivoire", "cote d'ivoire", "ivory coast", "ivorian*", "abidjan", "elfenbeinküste", "costa d'avorio", "costa de marfil", "costa do marfim"]),
     ("sn", "Senegal", ["senegal", "sénégal", "senegalese", "dakar"]),
     ("ml", "Mali", ["mali", "malian", "bamako"]),
     ("bf", "Burkina Faso", ["burkina faso", "burkinabe", "burkinabè"]),
     ("ne", "Niger", ["niger republic", "nigerien", "niamey"]),
     ("lr", "Liberia", ["liberia", "liberian*"]),
     ("sl", "Sierra Leone", ["sierra leone", "sierra leonean*"]),
     ("gn", "Guinea", ["guinea conakry", "guinean", "conakry", "guinée"]),
     ("gw", "Guinea-Bissau", ["guinea-bissau", "guinée-bissau"]),
     ("gm", "Gambia", ["gambia", "gambian*"]),
     ("tg", "Togo", ["togo", "togolese"]),
     ("bj", "Benin", ["republic of benin", "beninese", "bénin", "cotonou"]),
     ("mr", "Mauritania", ["mauritania", "mauritanian*", "mauritanie", "nouakchott"]),
   ]),
   ("af-c", "Central Africa", [
     ("cd", "DR Congo", ["democratic republic of congo", "democratic republic of the congo", "dr congo", "drc", "congolese", "kinshasa", "kivu", "katanga", "kolwezi", "lualaba", "rd congo", "rdc"]),
     ("cg", "Republic of Congo", ["republic of congo", "congo-brazzaville", "brazzaville"]),
     ("cm", "Cameroon", ["cameroon", "cameroonian*", "cameroun", "kamerun"]),
     ("ga", "Gabon", ["gabon", "gabonese"]),
     ("cf", "Central African Republic", ["central african republic", "centrafrique"]),
     ("td", "Chad", ["chadian", "tchad", "n'djamena"]),
     ("gq", "Equatorial Guinea", ["equatorial guinea", "guinea ecuatorial"]),
   ]),
   ("af-e", "East Africa & Horn", [
     ("ke", "Kenya", ["kenya", "kenyan*", "nairobi", "mombasa"]),
     ("ug", "Uganda", ["uganda", "ugandan*", "kampala"]),
     ("tz", "Tanzania", ["tanzania", "tanzanian*", "dar es salaam", "zanzibar"]),
     ("et", "Ethiopia", ["ethiopia", "ethiopian*", "addis ababa", "äthiopien", "etiopía", "etiópia"]),
     ("er", "Eritrea", ["eritrea", "eritrean*", "asmara"]),
     ("so", "Somalia", ["somalia", "somali", "somalis", "somaliland", "mogadishu"]),
     ("dj", "Djibouti", ["djibouti"]),
     ("rw", "Rwanda", ["rwanda", "rwandan*"]),
     ("bi", "Burundi", ["burundi", "burundian*"]),
     ("sd", "Sudan", ["sudan", "sudanese", "khartoum", "darfur", "soudan"]),
     ("ss", "South Sudan", ["south sudan", "south sudanese", "juba"]),
     ("mg", "Madagascar", ["madagascar", "malagasy"]),
   ]),
   ("af-s", "Southern Africa", [
     ("za", "South Africa", ["south africa", "south african*", "johannesburg", "cape town", "durban", "sudáfrica", "afrique du sud", "südafrika", "sudafrica", "áfrica do sul"]),
     ("zw", "Zimbabwe", ["zimbabwe", "zimbabwean*", "harare"]),
     ("zm", "Zambia", ["zambia", "zambian*", "lusaka"]),
     ("mw", "Malawi", ["malawi", "malawian*", "lilongwe"]),
     ("mz", "Mozambique", ["mozambique", "mozambican*", "moçambique", "mosambik", "maputo"]),
     ("ao", "Angola", ["angola", "angolan*", "luanda"]),
     ("na", "Namibia", ["namibia", "namibian*"]),
     ("bw", "Botswana", ["botswana"]),
     ("ls", "Lesotho", ["lesotho"]),
     ("sz", "Eswatini", ["eswatini", "swaziland"]),
     ("mu", "Mauritius", ["mauritius", "mauritian*"]),
   ]),
   ("af-n", "North Africa", [
     ("ma", "Morocco", ["morocco", "moroccan*", "maroc", "marruecos", "marokko", "marocco", "marrocos", "rabat", "casablanca"]),
     ("dz", "Algeria", ["algeria", "algerian*", "algérie", "argelia", "algerien", "algiers"]),
     ("tn", "Tunisia", ["tunisia", "tunisian*", "tunisie", "túnez", "tunesien", "tunis", "sfax"]),
     ("ly", "Libya", ["libya", "libyan*", "libye", "libia", "libyen", "tripoli", "benghazi"]),
     ("eg", "Egypt", ["egypt", "egyptian*", "égypte", "egipto", "ägypten", "egitto", "cairo"]),
   ]),
 ]),
 ("mideast", "Middle East", [
   ("me-g", "Gulf states & Yemen", [
     ("qa", "Qatar", ["qatar", "qatari*", "katar", "doha"]),
     ("sa", "Saudi Arabia", ["saudi arabia", "saudi", "saudis", "arabia saudita", "arabia saudí", "arabie saoudite", "saudi-arabien", "arábia saudita", "riyadh", "jeddah", "neom"]),
     ("ae", "United Arab Emirates", ["united arab emirates", "uae", "emirati*", "emirates", "dubai", "abu dhabi", "emiratos", "émirats"]),
     ("kw", "Kuwait", ["kuwait", "kuwaiti*"]),
     ("bh", "Bahrain", ["bahrain", "bahraini*"]),
     ("om", "Oman", ["oman", "omani*", "muscat"]),
     ("ye", "Yemen", ["yemen", "yemeni*"]),
   ]),
   ("me-l", "Levant, Iraq & Iran", [
     ("lb", "Lebanon", ["lebanon", "lebanese", "beirut", "líbano", "liban", "libanon"]),
     ("jo", "Jordan", ["jordan", "jordanian*", "amman"]),
     ("sy", "Syria", ["syria", "syrian*", "damascus", "siria", "syrie", "syrien"]),
     ("iq", "Iraq", ["iraq", "iraqi*", "baghdad", "kurdistan region", "irak"]),
     ("ir", "Iran", ["iran", "iranian*", "tehran"]),
     ("il", "Israel & Palestine", ["israel", "israeli*", "palestinian*", "west bank", "gaza", "tel aviv"]),
   ]),
   ("me-t", "Turkey", [
     ("tr", "Turkey", ["turkey", "türkiye", "turkish", "istanbul", "ankara", "turquía", "turquie", "türkei", "turchia", "turquia"]),
   ]),
 ]),
 ("asia-s", "South Asia", [
   ("sa-in", "India", [
     ("in", "India", ["india", "indian", "indians", "inde", "indien", "índia", "new delhi", "delhi", "mumbai", "tamil nadu", "uttar pradesh", "bihar", "jharkhand", "odisha", "rajasthan", "punjab india", "andhra pradesh", "telangana", "karnataka", "kerala", "maharashtra", "gujarat", "west bengal", "assam"]),
   ]),
   ("sa-pa", "Pakistan & Afghanistan", [
     ("pk", "Pakistan", ["pakistan", "pakistani*", "lahore", "karachi", "islamabad", "sindh"]),
     ("af", "Afghanistan", ["afghanistan", "afghan*", "kabul"]),
   ]),
   ("sa-o", "Bangladesh, Nepal & Sri Lanka", [
     ("bd", "Bangladesh", ["bangladesh", "bangladeshi*", "dhaka", "chittagong", "rohingya"]),
     ("np", "Nepal", ["nepal", "nepali*", "nepalese", "kathmandu"]),
     ("lk", "Sri Lanka", ["sri lanka", "sri lankan*", "colombo"]),
     ("bt", "Bhutan", ["bhutan"]),
     ("mv", "Maldives", ["maldives", "maldivian*"]),
   ]),
 ]),
 ("asia-se", "Southeast Asia", [
   ("se-m", "Mainland Southeast Asia", [
     ("mm", "Myanmar", ["myanmar", "burma", "burmese", "yangon", "myawaddy", "shwe kokko", "kk park", "karen state", "shan state"]),
     ("th", "Thailand", ["thailand", "thai", "bangkok", "mae sot", "tailandia", "thaïlande"]),
     ("kh", "Cambodia", ["cambodia", "cambodian*", "phnom penh", "sihanoukville", "camboya", "cambodge", "kambodscha"]),
     ("la", "Laos", ["laos", "lao pdr", "laotian*", "golden triangle"]),
     ("vn", "Vietnam", ["vietnam", "viet nam", "vietnamese", "hanoi", "ho chi minh"]),
   ]),
   ("se-i", "Maritime Southeast Asia", [
     ("my", "Malaysia", ["malaysia", "malaysian*", "kuala lumpur", "sabah", "sarawak", "penang"]),
     ("id", "Indonesia", ["indonesia", "indonesian*", "jakarta", "java", "sumatra", "kalimantan", "sulawesi", "papua"]),
     ("ph", "Philippines", ["philippines", "philippine", "filipino*", "filipina*", "manila", "mindanao", "pogo"]),
     ("sg", "Singapore", ["singapore", "singaporean*"]),
     ("tl", "Timor-Leste", ["timor-leste", "east timor"]),
     ("bn", "Brunei", ["brunei"]),
   ]),
 ]),
 ("asia-e", "East Asia", [
   ("ea-c", "China, Hong Kong & Taiwan", [
     ("cn", "China", ["china", "chinese", "beijing", "xinjiang", "uyghur*", "uighur*", "tibet", "tibetan*", "chine", "cina", "中国", "中國", "新疆"]),
     ("hk", "Hong Kong", ["hong kong", "香港"]),
     ("tw", "Taiwan", ["taiwan", "taiwanese", "taipei", "台灣", "台湾"]),
     ("mo", "Macao", ["macao", "macau", "澳門", "澳门"]),
   ]),
   ("ea-j", "Japan & Korea", [
     ("jp", "Japan", ["japan", "japanese", "tokyo", "japón", "japon", "giappone", "日本"]),
     ("kr", "South Korea", ["south korea", "south korean*", "seoul", "corea del sur", "corée du sud", "südkorea", "한국"]),
     ("kp", "North Korea", ["north korea", "north korean*", "dprk", "pyongyang", "corea del norte", "nordkorea", "북한"]),
   ]),
 ]),
 ("asia-c", "Central Asia & Caucasus", [
   ("ca-c", "Central Asia", [
     ("uz", "Uzbekistan", ["uzbekistan", "uzbek*", "tashkent"]),
     ("tm", "Turkmenistan", ["turkmenistan", "turkmen"]),
     ("kz", "Kazakhstan", ["kazakhstan", "kazakh*", "almaty", "astana"]),
     ("kg", "Kyrgyzstan", ["kyrgyzstan", "kyrgyz"]),
     ("tj", "Tajikistan", ["tajikistan", "tajik*"]),
     ("mn", "Mongolia", ["mongolia", "mongolian*"]),
   ]),
   ("ca-k", "Caucasus", [
     ("ge", "Georgia", ["republic of georgia", "tbilisi"]),
     ("am", "Armenia", ["armenia", "armenian*", "yerevan"]),
     ("az", "Azerbaijan", ["azerbaijan", "azerbaijani*", "baku"]),
   ]),
 ]),
 ("europe", "Europe", [
   ("eu-w", "Western Europe", [
     ("gb", "United Kingdom", ["united kingdom", "uk", "britain", "great britain", "british", "england", "scotland", "scottish", "wales", "welsh", "northern ireland", "london", "manchester", "birmingham", "reino unido", "royaume-uni", "großbritannien", "regno unito"]),
     ("ie", "Ireland", ["ireland", "irish", "dublin", "irlanda", "irlande", "irland"]),
     ("fr", "France", ["france", "french", "paris", "marseille", "francia", "frankreich", "frança"]),
     ("be", "Belgium", ["belgium", "belgian*", "belgique", "belgië", "bélgica", "belgien", "belgio", "antwerp"]),
     ("nl", "Netherlands", ["netherlands", "dutch", "holland", "amsterdam", "rotterdam", "nederland", "países bajos", "pays-bas", "niederlande", "paesi bassi"]),
     ("lu", "Luxembourg", ["luxembourg", "luxemburg"]),
     ("de", "Germany", ["germany", "german", "berlin", "munich", "hamburg", "deutschland", "alemania", "allemagne", "germania", "alemanha"]),
     ("at", "Austria", ["austria", "austrian*", "vienna", "österreich", "autriche", "áustria"]),
     ("ch", "Switzerland", ["switzerland", "swiss", "geneva", "zurich", "schweiz", "suisse", "svizzera", "suiza", "suíça"]),
   ]),
   ("eu-s", "Southern Europe", [
     ("it", "Italy", ["italy", "italian*", "italia", "italie", "italien", "itália", "rome", "milan", "milano", "naples", "napoli", "sicily", "sicilia", "puglia", "apulia", "calabria", "campania", "lazio", "latina", "foggia", "agro pontino", "piemonte", "veneto", "lombardia", "toscana", "emilia-romagna", "basilicata", "sardegna", "prato"]),
     ("es", "Spain", ["spain", "spanish", "españa", "espana", "espagne", "spanien", "spagna", "espanha", "madrid", "barcelona", "almería", "almeria", "huelva", "murcia", "andalucía", "andalusia"]),
     ("pt", "Portugal", ["portugal", "portuguese", "lisbon", "lisboa", "alentejo", "odemira"]),
     ("gr", "Greece", ["greece", "greek", "athens", "grecia", "grèce", "griechenland", "grécia", "manolada"]),
     ("mt", "Malta", ["malta", "maltese"]),
     ("cy", "Cyprus", ["cyprus", "cypriot*", "chipre", "chypre", "zypern", "cipro"]),
   ]),
   ("eu-n", "Nordic & Baltic", [
     ("se", "Sweden", ["sweden", "swedish", "sverige", "suecia", "suède", "schweden", "svezia", "suécia", "stockholm"]),
     ("no", "Norway", ["norway", "norwegian*", "norge", "noruega", "norvège", "norwegen", "norvegia", "oslo"]),
     ("dk", "Denmark", ["denmark", "danish", "danmark", "dinamarca", "danemark", "dänemark", "danimarca", "copenhagen"]),
     ("fi", "Finland", ["finland", "finnish", "suomi", "finlandia", "finlande", "finnland", "finlândia", "helsinki"]),
     ("is", "Iceland", ["iceland", "icelandic"]),
     ("ee", "Estonia", ["estonia", "estonian*"]),
     ("lv", "Latvia", ["latvia", "latvian*"]),
     ("lt", "Lithuania", ["lithuania", "lithuanian*"]),
   ]),
   ("eu-e", "Central & Eastern Europe", [
     ("pl", "Poland", ["poland", "polish", "polska", "polonia", "pologne", "polen", "polônia", "warsaw"]),
     ("cz", "Czechia", ["czechia", "czech republic", "czech", "prague"]),
     ("sk", "Slovakia", ["slovakia", "slovak"]),
     ("hu", "Hungary", ["hungary", "hungarian*", "budapest", "ungarn", "hongrie", "hungría", "ungheria"]),
     ("ro", "Romania", ["romania", "romanian*", "rumania", "roumanie", "rumänien", "romênia", "bucharest"]),
     ("bg", "Bulgaria", ["bulgaria", "bulgarian*", "bulgarie", "bulgarien"]),
     ("rs", "Serbia", ["serbia", "serbian*", "belgrade", "serbien", "serbie"]),
     ("hr", "Croatia", ["croatia", "croatian*", "kroatien", "croatie", "croacia", "croazia"]),
     ("si", "Slovenia", ["slovenia", "slovenian*"]),
     ("ba", "Bosnia & Herzegovina", ["bosnia", "bosnian*", "sarajevo"]),
     ("me", "Montenegro", ["montenegro"]),
     ("mk", "North Macedonia", ["north macedonia", "macedonia"]),
     ("al", "Albania", ["albania", "albanian*", "tirana"]),
     ("xk", "Kosovo", ["kosovo"]),
     ("md", "Moldova", ["moldova", "moldovan*", "chisinau"]),
     ("ua", "Ukraine", ["ukraine", "ukrainian*", "kyiv", "ucrania", "ukraina", "ucraina", "україна"]),
     ("by", "Belarus", ["belarus", "belarusian*", "minsk"]),
     ("ru", "Russia", ["russia", "russian*", "moscow", "rusia", "russie", "russland", "rússia", "россия", "росси"]),
   ]),
   ("eu-x", "EU institutions", [
     ("eu", "European Union", ["european union", "european commission", "european parliament", "council of the eu", "eu forced labour regulation", "eu forced labor regulation", "csddd", "unión europea", "union européenne", "europäische union", "unione europea", "união europeia"]),
   ]),
 ]),
 ("americas-n", "North America", [
   ("na-uc", "United States & Canada", [
     ("us", "United States", ["united states", "u.s.", "usa", "u.s.a.", "american*", "america", "washington", "new york", "california", "texas", "florida", "estados unidos", "états-unis", "vereinigte staaten", "stati uniti", "eeuu", "ee.uu."]),
     ("ca", "Canada", ["canada", "canadian*", "ontario", "quebec", "québec", "british columbia", "alberta", "toronto", "vancouver", "canadá", "kanada"]),
   ]),
 ]),
 ("americas-s", "Latin America & Caribbean", [
   ("la-c", "Mexico & Central America", [
     ("mx", "Mexico", ["mexico", "méxico", "mexican*", "mexique", "mexiko", "messico", "méxico", "chiapas", "sinaloa", "jalisco", "chihuahua", "baja california"]),
     ("gt", "Guatemala", ["guatemala", "guatemalan*"]),
     ("hn", "Honduras", ["honduras", "honduran*"]),
     ("sv", "El Salvador", ["el salvador", "salvadoran*"]),
     ("ni", "Nicaragua", ["nicaragua", "nicaraguan*"]),
     ("cr", "Costa Rica", ["costa rica", "costa rican*"]),
     ("pa", "Panama", ["panama", "panamá", "panamanian*"]),
     ("bz", "Belize", ["belize"]),
   ]),
   ("la-car", "Caribbean & Guianas", [
     ("ht", "Haiti", ["haiti", "haití", "haïti", "haitian*"]),
     ("do", "Dominican Republic", ["dominican republic", "república dominicana", "dominican*"]),
     ("cu", "Cuba", ["cuba", "cuban*"]),
     ("jm", "Jamaica", ["jamaica", "jamaican*"]),
     ("tt", "Trinidad & Tobago", ["trinidad"]),
     ("gy", "Guyana", ["guyana", "guyanese"]),
     ("sr", "Suriname", ["suriname"]),
   ]),
   ("la-a", "Andean countries", [
     ("co", "Colombia", ["colombia", "colombian*", "colombie", "kolumbien", "bogotá", "bogota", "medellín"]),
     ("ve", "Venezuela", ["venezuela", "venezuelan*"]),
     ("ec", "Ecuador", ["ecuador", "ecuadorian*", "équateur", "equador"]),
     ("pe", "Peru", ["peru", "perú", "peruvian*", "pérou", "lima"]),
     ("bo", "Bolivia", ["bolivia", "bolivian*", "bolivie", "bolívia"]),
   ]),
   ("la-s", "Brazil & the Southern Cone", [
     ("br", "Brazil", ["brazil", "brazilian*", "brasil", "brésil", "brasilien", "brasile", "são paulo", "sao paulo", "rio de janeiro", "minas gerais", "bahia", "pará", "mato grosso", "goiás", "rio grande do sul", "santa catarina", "paraná", "maranhão", "piauí", "amazonas", "espírito santo", "pernambuco", "ceará", "tocantins"]),
     ("ar", "Argentina", ["argentina", "argentine", "argentinian*", "argentinien", "buenos aires", "mendoza"]),
     ("cl", "Chile", ["chile", "chilean*", "chili", "santiago de chile"]),
     ("uy", "Uruguay", ["uruguay", "uruguayan*", "uruguai"]),
     ("py", "Paraguay", ["paraguay", "paraguayan*", "paraguai", "asunción"]),
   ]),
 ]),
 ("oceania", "Oceania", [
   ("oc-a", "Australia & New Zealand", [
     ("au", "Australia", ["australia", "australian*", "sydney", "melbourne", "queensland", "new south wales", "victoria australia", "austrália", "australie", "australien"]),
     ("nz", "New Zealand", ["new zealand", "aotearoa", "nueva zelanda", "nouvelle-zélande", "neuseeland"]),
   ]),
   ("oc-p", "Pacific Islands", [
     ("pg", "Papua New Guinea", ["papua new guinea"]),
     ("fj", "Fiji", ["fiji", "fijian*"]),
     ("sb", "Solomon Islands", ["solomon islands"]),
     ("vu", "Vanuatu", ["vanuatu"]),
     ("ws", "Samoa", ["samoa", "samoan*"]),
     ("to", "Tonga", ["tonga", "tongan*"]),
     ("ki", "Kiribati", ["kiribati"]),
     ("mh", "Marshall Islands", ["marshall islands"]),
     ("fm", "Micronesia", ["micronesia"]),
     ("pw", "Palau", ["palau"]),
   ]),
 ]),
]
GLOBAL_REGION = ("global", "International / several countries")

# Centres for regions and subregions; country points come from the gazetteer's
# own table, so only these are written out here.
COORDS = {
 "africa": [3.0, 20.0], "mideast": [27.0, 45.0], "asia-s": [24.0, 78.0], "asia-se": [8.0, 108.0],
 "asia-e": [33.0, 118.0], "asia-c": [43.0, 62.0], "europe": [50.0, 14.0],
 "americas-n": [45.0, -98.0], "americas-s": [-12.0, -62.0], "oceania": [-25.0, 145.0],
 "af-w": [11.0, -5.0], "af-c": [1.0, 18.0], "af-e": [3.0, 38.0], "af-s": [-20.0, 28.0],
 "af-n": [29.0, 10.0], "me-g": [23.0, 50.0], "me-l": [33.0, 42.0], "me-t": [39.0, 35.0],
 "sa-in": [22.0, 79.0], "sa-pa": [31.0, 68.0], "sa-o": [24.0, 86.0],
 "se-m": [16.0, 102.0], "se-i": [1.0, 114.0], "ea-c": [32.0, 108.0], "ea-j": [36.0, 133.0],
 "ca-c": [43.0, 64.0], "ca-k": [41.0, 45.0], "eu-w": [50.0, 4.0], "eu-s": [41.0, 10.0],
 "eu-n": [61.0, 18.0], "eu-e": [49.0, 25.0], "eu-x": [50.85, 4.35],
 "na-uc": [45.0, -98.0], "la-c": [17.0, -92.0], "la-car": [15.0, -70.0], "la-a": [-5.0, -74.0],
 "la-s": [-22.0, -55.0], "oc-a": [-30.0, 150.0], "oc-p": [-12.0, 165.0],
 "global": [20.0, 10.0],
}
if _GAZETTEER:
    for _rid, _rl, _subs in GEO3:
        for _sid, _sl, _places in _subs:
            for _pid, _pl, _t in _places:
                _pt = galaxy_places.LOCALE.get(_pid.upper())
                if _pt:
                    COORDS.setdefault(_pid, [_pt[1], _pt[2]])

# --------------------------------------------------------------------------
# Compiled forms.
# --------------------------------------------------------------------------
ON_C = [(_compile(t), _compile_all(g) if g else None) for t, g in ON]
OFF_C = _compile_all(OFF)
HARM_C = _compile_all(HARM)
ACTION_C = _compile_all(ACTION)
DOCUMENTED_C = _compile_all(DOCUMENTED)
INSTITUTIONAL_C = _compile_all(INSTITUTIONAL)
MEASURED_C = _compile_all(MEASURED)
PROJECTED_C = _compile_all(PROJECTED)
GLOBAL_C = _compile_all(WIRE_GLOBAL)
TOPICS_C = [(tid, label, _compile_all(terms)) for tid, label, terms in TOPICS]

# every place term, longest first, so "south sudan" is matched and removed
# before "sudan" is looked for, and "papua new guinea" before "papua"
PLACE_TERMS = sorted(
    ((term, _compile(term), rid, sid, pid)
     for rid, _rl, subs in GEO3 for sid, _sl, places in subs
     for pid, _pl, terms in places for term in terms),
    key=lambda row: -len(row[0]))

PLACE_LABEL = {}
ISO_REGION = {}
for _rid, _rlabel, _subs in GEO3:
    PLACE_LABEL[_rid] = _rlabel
    for _sid, _slabel, _places in _subs:
        PLACE_LABEL[_sid] = _slabel
        for _pid, _plabel, _terms in _places:
            PLACE_LABEL[_pid] = _plabel
            ISO_REGION.setdefault(_pid.lower(), (_rid, _sid, _pid))
PLACE_LABEL[GLOBAL_REGION[0]] = GLOBAL_REGION[1]
# gazetteer codes this table files under a neighbouring entry
for _cc, _to in (("ps", "il"), ("pr", "us"), ("gu", "us")):
    ISO_REGION.setdefault(_cc, ISO_REGION[_to])


def _on_hits(text, context=None):
    """The distinct subject terms present in text. A guarded term counts when
    one of its guards is in the context (the whole story, by default)."""
    context = text if context is None else context
    found = set()
    for i, (term, guards) in enumerate(ON_C):
        if not hit(text, [term]):
            continue
        if guards and not hit(context, guards):
            continue
        found.add(i)
    return found


def gate(title, text):
    """'in', 'refused' (on the subject but carrying an OFF phrase), or None."""
    hits = _on_hits(text)
    if not hits:
        return None
    if hit(text, OFF_C):
        return "refused"
    if _on_hits(title.lower(), text):
        return "in"
    return "in" if len(hits) >= 2 else None


def kind_of(text):
    """Harm reported, action taken, or both."""
    kinds = []
    if hit(text, HARM_C):
        kinds.append("harm")
    if hit(text, ACTION_C):
        kinds.append("action")
    return kinds or ["harm"]


def pressure(text, standing, placed):
    total, reasons = 0, []
    if hit(text, DOCUMENTED_C):
        total += 2
        reasons.append("documented")
    if hit(text, INSTITUTIONAL_C):
        total += 2
        reasons.append("institutional")
    if hit(text, MEASURED_C) or MEASURED_RE.search(text) or MEASURED_SIGN_RE.search(text):
        total += 1
        reasons.append("measured")
    if hit(text, PROJECTED_C):
        total += 1
        reasons.append("projected")
    if placed:
        total += 1
        reasons.append("located")
    if standing in ("official", "research", "labour"):
        total += 1
        reasons.append("primary source")
    return total, reasons


def topics_for(text):
    hits = [tid for tid, _label, terms in TOPICS_C if hit(text, terms)]
    return hits or [OTHER_TOPIC[0]]


def _blank(text, c):
    """Remove every match of c from text, keeping offsets."""
    if isinstance(c, str):
        return text.replace(c, " " * len(c))
    return c.sub(lambda m: " " * len(m.group(0)), text)


def places_for(text):
    """Returns (regions, subregions, places). Naming a place implies the
    subregion and region above it. Longer names are matched first and removed,
    so a story about South Sudan is not also filed under Sudan."""
    work = " " + text + " "
    for ph in GEO_STRIP:
        work = work.replace(ph, " " * len(ph))
    regions, subs, places = [], [], []
    for _term, c, rid, sid, pid in PLACE_TERMS:
        if not hit(work, [c]):
            continue
        work = _blank(work, c)
        if pid not in places:
            places.append(pid)
        if sid not in subs:
            subs.append(sid)
        if rid not in regions:
            regions.append(rid)
    return (regions or ["unlocated"], subs or ["unlocated"], places or ["unlocated"])


def is_multi(text, places):
    """Several countries at once, the map's _isMulti test."""
    if hit(text, GLOBAL_C):
        return True
    return len([p for p in places if p not in ("unlocated", "eu")]) >= 2


def file_by_country(row, cc):
    """Put a gazetteer-placed story under its region, subregion and country."""
    if not cc:
        return
    found = ISO_REGION.get(str(cc).lower())
    if not found:
        return
    rid, sid, pid = found
    if not row.get("w") or row["w"] in (["unlocated"], ["global"]):
        row["w"] = [rid] + (["global"] if row.get("w") == ["global"] else [])
    if not row.get("sr") or row["sr"] == ["unlocated"]:
        row["sr"] = [sid]
    if not row.get("pl") or row["pl"] == ["unlocated"]:
        row["pl"] = [pid]


def point_for(places, subs, regions, locale=None, raw=None):
    """The most specific point a story resolved to.

    A settlement named in the story wins, from the shared gazetteer. Then the
    country this wire's own table placed it in, then its subregion and region.
    Then any division or country the gazetteer found. Last, and weakest, the
    country the source itself reports from: that point is marked approximate,
    and the page draws it hollow.

    Returns (label_or_None, point_or_None, approx, iso_or_None).
    """
    glabel, gpoint, grank, gcc = None, None, -1, None
    if _GAZETTEER:
        glabel, gpoint, grank, _approx, gcc = galaxy_places.resolve_full(raw or "")
        if grank == 3:
            return glabel, gpoint, False, gcc

    for level in (places, subs, regions):
        for pid in level:
            if pid in COORDS and pid not in ("unlocated", "global"):
                return PLACE_LABEL.get(pid), COORDS[pid], False, None

    if gpoint:
        return glabel, gpoint, False, gcc

    if _GAZETTEER and locale:
        llabel, lpoint, _lrank, lapprox, _lcc = galaxy_places.resolve_full("", locale)
        if lpoint:
            return llabel, lpoint, lapprox, None

    return None, None, False, None


def classify(row, src):
    """Tag a parsed row in place. Returns 'in', 'refused' or None."""
    # " | " between headline and snippet, so that no phrase is read across the
    # join: Google News snippets restart with the headline, and "...Forced
    # Labour" + "MPs call..." would otherwise read as "labour mps".
    text = (row["t"] + " | " + (row.get("s") or "")).lower()
    verdict = gate(row["t"], text)
    if verdict != "in":
        return verdict
    regions, subs, places = places_for(text)
    if is_multi(text, places):
        regions = [r for r in regions if r != "unlocated"] + [GLOBAL_REGION[0]]
    row["x"] = topics_for(text)
    row["w"] = regions
    row["sr"] = subs
    row["pl"] = places
    row["gl"] = src.get("gl")
    _raw = (row["t"] or "") + " " + (row.get("s") or "")
    row["pn"], row["ll"], row["pa"], cc = point_for(places, subs, regions, src.get("gl"), _raw)
    # Only a point read out of the story files it under a country. A point
    # standing in for the reporting locale does not: with most English queries
    # asked from the US edition, that would file every unplaced story there.
    if row["ll"] and not row["pa"]:
        file_by_country(row, cc)
    total, reasons = pressure(text, src["standing"],
                              [r for r in row["w"] if r not in ("unlocated", "global")] != [])
    row["p"] = total
    row["y"] = reasons
    row["st"] = src["standing"]
    row["k"] = kind_of(text)
    return "in"


def load_sources():
    with open(SOURCES_PATH, encoding="utf-8") as fh:
        cfg = json.load(fh)
    srcs = []
    for s in cfg.get("direct", []):
        srcs.append({"name": s["name"], "lang": s["lang"], "standing": s["standing"],
                     "region": s["standing"], "kind": s.get("kind", "news"), "url": s["url"]})
    for block, prefix in (("gnews", "Google News · "), ("events", "Search · ")):
        for loc in cfg.get(block, []):
            srcs.append({"name": prefix + loc["label"], "lang": loc["lang"],
                         "standing": loc["standing"], "region": loc["standing"],
                         "kind": "news", "url": build_gnews_url(loc), "gl": loc.get("gl"),
                         "was": loc.get("was")})
    return srcs, cfg


def rows_from_map_wire(path, sources):
    """The anti-slavery map's wire.json, as rows this harvester would have
    parsed itself. Each item is matched back to the wire it came from, so it
    gets that wire's language, standing and locale; titles are split from the
    outlet name Google News appends, exactly as parse_feed does, and are
    otherwise left as they were published."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("items", [])
    by_name = {}
    for s in sources:
        by_name[s["name"]] = s
        if s.get("was"):
            by_name[s["was"]] = s
    out = []
    for it in data:
        name = it.get("name") or ""
        src = by_name.get(name) or {"name": name, "lang": it.get("lang", "en"),
                                    "standing": "press", "region": "press", "kind": "news"}
        title, outlet = split_outlet(it.get("title") or "", "", "Search", it.get("link") or "")
        out.append((src, {
            "t": title,
            "u": it.get("link") or "",
            "o": outlet or re.sub(r"^(Google News|Search) · ", "", src["name"]),
            "g": src.get("lang") or it.get("lang", "en"),
            "r": src["standing"],
            "k": "news",
            "d": parse_date(it.get("date") or ""),
            "s": (it.get("snippet") or "")[:SNIPPET_CHARS],
            "w": src["name"],
        }))
    return out


def run(dry_run=False, fixtures=None, from_json=None):
    global DEADLINE
    sources, cfg = load_sources()

    seen_fp, seen_url, items = set(), set(), []

    def absorb(row):
        fp = fingerprint(row["t"])
        cu = canon_url(row["u"])
        if fp in seen_fp or cu in seen_url:
            return False
        seen_fp.add(fp)
        seen_url.add(cu)
        items.append(row)
        return True

    stats, ok_count, refused = [], 0, 0
    stat_by_name = {}
    for src in sources:
        stat = {"name": src["name"], "lang": src["lang"], "standing": src["standing"],
                "region": src["standing"], "kept": 0, "refused": 0, "ok": False}
        stats.append(stat)
        stat_by_name[src["name"]] = stat

    previous = []
    if from_json:
        print("Re-tagging %s…" % from_json)
        for src, row in rows_from_map_wire(from_json, sources):
            stat = stat_by_name.get(src["name"])
            if stat is None:
                stat = {"name": src["name"], "lang": src["lang"], "standing": src["standing"],
                        "region": src["standing"], "kept": 0, "refused": 0, "ok": False}
                stats.append(stat)
                stat_by_name[src["name"]] = stat
            stat["ok"] = True
            verdict = classify(row, src)
            if verdict == "refused":
                stat["refused"] += 1
                refused += 1
            elif verdict == "in" and absorb(row):
                stat["kept"] += 1
        ok_count = sum(1 for s in stats if s["ok"])
        for s in stats:
            if not s["ok"]:
                s["pending"] = True          # not read yet: the seed did not include it
    else:
        if not fixtures:
            DEADLINE = time.monotonic() + READ_BUDGET_MIN * 60
        print("Reading %d wires…" % len(sources))

        def read(src):
            if fixtures:
                path = os.path.join(fixtures, re.sub(r"[^\w.-]", "_", src["name"]) + ".xml")
                if not os.path.exists(path):
                    return src, None
                with open(path, "rb") as fh:
                    return src, fh.read()
            return src, fetch(src["url"])

        results = []
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for src, raw in pool.map(read, sources):
                results.append((src, raw))

        if os.path.exists(OUT_PATH):
            try:
                with open(OUT_PATH, encoding="utf-8") as fh:
                    previous = json.load(fh).get("items", [])
            except Exception:  # noqa: BLE001
                previous = []

        for src, raw in results:
            stat = stat_by_name[src["name"]]
            if raw:
                stat["ok"] = True
                ok_count += 1
                for row in parse_feed(raw, src):
                    verdict = classify(row, src)
                    if verdict == "refused":
                        stat["refused"] += 1
                        refused += 1
                    elif verdict == "in" and absorb(row):
                        stat["kept"] += 1
            print("  %-36s %s" % (src["name"][:36],
                                  "unreachable" if not raw
                                  else "%d kept, %d refused" % (stat["kept"], stat["refused"])))

    fresh_urls = {canon_url(i["u"]) for i in items}
    for row in previous:
        if "x" not in row:
            continue
        # A retained story is placed again when it has no point, or only the
        # reporting-locale one, so a change to placement reaches old rows too.
        if not row.get("ll") or row.get("pa"):
            _raw = ((row.get("t") or "") + " " + (row.get("s") or ""))
            row["pn"], row["ll"], row["pa"], cc = point_for(
                row.get("pl") or [], row.get("sr") or [], row.get("w") or [], row.get("gl"), _raw)
            if row["ll"] and not row["pa"]:
                file_by_country(row, cc)
        absorb(row)

    if not from_json:
        cutoff = int(time.time() * 1000) - RETAIN_DAYS * 86400000
        items = [i for i in items if (i.get("d") or cutoff + 1) >= cutoff]
    items.sort(key=lambda i: i.get("d") or 0, reverse=True)
    items = items[:MAX_ITEMS]
    fresh = sum(1 for i in items if canon_url(i["u"]) in fresh_urls)

    languages = {}
    for loc in cfg.get("gnews", []):
        languages.setdefault(loc["lang"], LANG_NAMES.get(loc["lang"]) or
                             re.sub(r"\s*·.*$|\s*\(.*$|\s+\d+$", "", loc["label"]).strip())
    for i in items:
        languages.setdefault(i["g"], LANG_NAMES.get(i["g"], i["g"]))
    languages.setdefault("en", "English")

    topics = [{"id": tid, "label": label} for tid, label, _ in TOPICS]
    topics.append({"id": OTHER_TOPIC[0], "label": OTHER_TOPIC[1]})

    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counts": {"stories": len(items), "new_this_run": fresh,
                   "languages": len({i["g"] for i in items}),
                   "notable": sum(1 for i in items if i.get("p", 0) >= NOTABLE_SCORE),
                   "harm": sum(1 for i in items if "harm" in i.get("k", [])),
                   "action": sum(1 for i in items if "action" in i.get("k", [])),
                   "refused": refused,
                   "wires_ok": ok_count, "wires_total": len(sources)},
        "notable_score": NOTABLE_SCORE,
        "languages": languages,
        "kinds": [
            {"id": "harm", "label": "Harm reported"},
            {"id": "action", "label": "Action taken"},
        ],
        "standings": [
            {"id": "official", "label": "Bodies & courts"},
            {"id": "labour", "label": "Anti-slavery & labour groups"},
            {"id": "research", "label": "Research & rights groups"},
            {"id": "field", "label": "Investigative & field press"},
            {"id": "press", "label": "Press"},
        ],
        "topics": topics,
        "coords": COORDS,
        "geo": ([{"id": rid, "label": rlabel,
                  "subs": [{"id": sid, "label": slabel,
                            "places": [{"id": pid, "label": plabel} for pid, plabel, _t in places]}
                           for sid, slabel, places in subs]}
                 for rid, rlabel, subs in GEO3] +
                [{"id": GLOBAL_REGION[0], "label": GLOBAL_REGION[1], "subs": []},
                 {"id": "unlocated", "label": "No single region", "subs": []}]),
        "sources": stats,
        "items": items,
    }
    if from_json:
        payload["seed"] = {"from": os.path.basename(from_json),
                           "note": "Re-tagged from the anti-slavery map's wire.json before the "
                                   "first harvest. The first Action run replaces it."}
        if items:
            payload["generated"] = datetime.fromtimestamp(
                max(i.get("d") or 0 for i in items) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("\n%d stories (%d new, %d well documented) · %d harm reported, %d action taken · %d refused · %d languages · %d/%d wires answered"
          % (len(items), fresh, payload["counts"]["notable"], payload["counts"]["harm"],
             payload["counts"]["action"], refused, payload["counts"]["languages"],
             ok_count, len(sources)))

    if dry_run:
        print("\n--dry-run: wire_slavery.json not written")
        return payload

    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print("Wrote %s (%.0f KB)" % (OUT_PATH, os.path.getsize(OUT_PATH) / 1024))
    return payload


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fixtures")
    ap.add_argument("--from-json", help="re-tag an existing wire.json (the map's) instead of reading wires")
    args = ap.parse_args()
    run(dry_run=args.dry_run, fixtures=args.fixtures, from_json=args.from_json)
