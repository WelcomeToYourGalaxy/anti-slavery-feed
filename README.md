# anti-slavery-feed

Forced labour, modern slavery, human trafficking and child labour, worldwide, in 24 languages. This
is the Global Wire of the anti-slavery map, run as its own site.

`harvest_slavery.py` runs every two hours in GitHub Actions, reads 173 wires, keeps what is about
the subject, marks whether a story reports harm or action taken, grades the evidence, tags it by
topic and region, and writes `wire_slavery.json`. `index.html` loads that file and renders it.

Nothing here rewrites a headline. Titles and snippets are the publishers' own, truncated but never
reworded, and every row keeps its original link. The one change is that the outlet name Google News
adds to the end of a headline (" - Reuters") is split off and shown beside the row. No model in the
pipeline, no API key, no paid service, no dependencies beyond the Python standard library.

## Where the definitions come from

The subject gate, the refusals, the eleven topic threads and the feed list are carried over from the
anti-slavery map: `harvest_wire.py` (its `ON` and `OFF` lists and its headline rule) and, in its
`index.html`, `WIRE_FEEDS`, `WIRE_THREADS`, `WIRE_OFF`, `WIRE_STOP` and `WIRE_GLOBAL`. Where a term
was changed, the reason is written beside `OFF` in `harvest_slavery.py`.

## Both directions

**Harm reported** — workers trafficked, wages withheld, passports confiscated, children in hazardous
work, crews abandoned at sea, deaths, allegations, investigations that find abuse.

**Action taken** — raids and rescues, arrests, charges and convictions, import bans and detained
shipments, entity-list additions, court rulings, back wages, compensation and settlements, laws
ratified or passed.

Every row is marked, and the *Direction* filter separates them. A story can be both: a raid that
freed forty workers reports the harm and the action. A story with neither kind of word, such as an
explainer on a new regulation, is filed as harm reported.

## Standing

| Standing | What it covers |
|---|---|
| Bodies & courts | UN News, OHCHR, JURIST, and searches of the ILO, US Labor Department, US Customs, UNODC |
| Anti-slavery & labour groups | Anti-Slavery International, Freedom United, Migrant-Rights.org, Equal Times, LabourStart, IndustriALL |
| Research & rights groups | Human Rights Watch, Amnesty International, Business & Human Rights Resource Centre, Walk Free |
| Investigative & field press | Reporter Brasil, Mongabay |
| Press | The Guardian, Al Jazeera, The Diplomat, Agência Brasil, and Google News in 24 languages |

Anti-slavery and labour groups are labelled so they can be read on their own.

## What gets in

The headline has to carry a subject term — forced labour, human trafficking, child labour, debt
bondage, bonded labour, servitude, kafala, recruitment fees, withhold release order, caporalato,
trabalho escravo, Menschenhandel and their equivalents in the other languages — or the headline and
snippet together have to carry two different ones. One term in the last line of a crime round-up is
not enough. This is the rule `harvest_wire.py` and the map use.

A few terms count only beside another word: *trafficked* needs people in the story (workers, women,
children, migrants, "trafficked into"), so drugs and weapons do not qualify; *slavery* on its own
needs a present-day act (rescued, raided, charged, convicted, police, workers).

Refused: the idioms ("slave to fashion", "wage slave"); the transatlantic slave trade, slavery museums
and plantation tours; commemoration of Nazi-era forced labour; the UK Labour Party; childbirth
("went into labour"); childcare policy; and sport, celebrity, markets and event promotion.

## Evidence

| Signal | Worth |
|---|---|
| A documented act: conviction, rescue, raid, arrest, seizure, ruling, fine, ban | 2 |
| Institutional material: ILO, UNICEF, UNODC, OHCHR, a court, US Customs, a labour inspectorate, Brazil's lista suja, a report or investigation | 2 |
| A measured count: "43 workers", a per cent, a sum of money | 1 |
| A pending decision or a date a rule takes effect | 1 |
| A named place | 1 |
| Primary source | 1 |

At **3** or more the row is marked well documented, and the *Evidence* filter narrows to those.

## Region, three levels deep

Placement runs region → subregion → country: 10 regions, 29 subregions, 180 countries and
territories. Middle East → Gulf states & Yemen → Qatar. Europe → Southern Europe → Italy. South Asia →
India → India. A story that names a city or province is placed through the shared gazetteer
(`galaxy_places.py` and `places.json.gz`, the same one the other feeds use): Foggia files under
Italy, Mae Sot under Thailand.

A story about several countries at once, or carrying words such as "cross-border", "worldwide" or
"transnational", is also filed under *International / several countries*, as the map's wire does.

Every chip carries its own count, so an empty subregion shows as empty rather than vanishing.

## Eleven topics

The map's threads, with the same ids: Raids, prosecutions & enforcement; Child labour; Supply chains
& sourcing; Import bans, customs & trade; Trafficking & recruitment; Migrant & contract workers;
Fishing, seafarers & work at sea; Mining, minerals & extraction; State-imposed & prison labour; Laws,
duties & courts; Survivors, remedy & compensation. A story none of them claims goes to *Other
reports*, as it does in the map's wire panel.

## Files

| File | Path in repo | What it is |
|---|---|---|
| `index.html` | `/index.html` | The feed page. Pages serves the repo root, so it must carry this name. |
| `harvest_slavery.py` | `/harvest_slavery.py` | The harvester. Self-contained. |
| `sources_slavery.json` | `/sources_slavery.json` | The wire list, with each wire's standing. |
| `wire_slavery.json` | `/wire_slavery.json` | The output the page reads. Starts as a seed (below). Never hand-edit. |
| `galaxy_places.py`, `places.json.gz` | `/` | The shared gazetteer. |
| `anti-slavery-feed-weebly-embed.html` | `/anti-slavery-feed-weebly-embed.html` | The page wrapped for a Weebly Embed Code element. |
| `verify_sources.py` | `/verify_sources.py` | Reports which wires answer and which are dead. Run it from the Actions tab. |
| `README.md` | `/README.md` | This file. |
| `harvest.yml` | `/.github/workflows/harvest.yml` | Runs every two hours at :13 and commits the wire. |
| `verify.yml` | `/.github/workflows/verify.yml` | The manual wire check. |

## The seed

Until the first Action run, `wire_slavery.json` holds the 321 stories from the anti-slavery map's
own `wire.json`, re-tagged by this harvester's gate, topics, direction, evidence and placement. 313
passed; three were refused (Nazi-era commemoration pieces) and five were duplicates. The page says
it is showing the seed, and the sources sheet marks the wires that have not been read yet. The first
harvest replaces it. To rebuild the seed:

```bash
python3 harvest_slavery.py --from-json path/to/anti-slavery-map/wire.json
```

## Setup

1. Push these files to the repository root.
2. Settings → Actions → General → Workflow permissions → **Read and write permissions**, save.
3. Actions tab → **Harvest the forced labour and trafficking wire** → *Run workflow*.
4. Settings → Pages → **Deploy from a branch**, branch `main`, folder `/ (root)`.
5. Confirm
   `https://raw.githubusercontent.com/WelcomeToYourGalaxy/anti-slavery-feed/main/wire_slavery.json`
   loads in a browser.

If the repository is named something else, change `REPO` near the top of the feed script in
`index.html` and regenerate the embed. The page also reads `wire_slavery.json` from beside itself
if neither GitHub address answers.

## Limits worth knowing

The gate reads words, not meaning. It misses stories that describe forced labour without using any
of its terms, and it lets in some it should not: German pieces on wartime forced labour that avoid
the refused phrases, for example. Add terms to `ON` or `OFF` in `harvest_slavery.py`; both lists are
meant to grow. The UK-politics refusals carried from the map ("labour government", "labour mp") also
refuse a story about that government's own modern slavery policy.

Most stories name no place the gazetteer recognises — in the seed, 180 of 313. Those are pinned
hollow at the country of the Google News edition that carried them and are not counted under any
region, because with most English queries asked from the US edition that would file them all under
the United States.

Standing is assigned per wire rather than per article. Google News caps a query at roughly 100
results over about 45 days. Coverage is uneven by language and the counts show it rather than
hiding it.

## Running it locally

```bash
python3 harvest_slavery.py              # full run
python3 harvest_slavery.py --dry-run    # harvest and report, write nothing
python3 verify_sources.py               # which wires are dead
```

Python 3.9 or later.
