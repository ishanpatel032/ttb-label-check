# Label check

A prototype that compares an alcohol beverage label image against the data in
its COLA application and shows a compliance agent which fields disagree.

Live prototype: _add your deployed URL here_

## Running it locally

Python 3.11 or newer.

```bash
git clone <this repo>
cd ttb-label-check

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # then add your ANTHROPIC_API_KEY
export $(grep -v '^#' .env | xargs)

uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000

Run the tests with `python tests/test_matching.py` or `python -m pytest`.

## Deploying

The repo includes `render.yaml`. On Render, create a new Blueprint from the
repo and set `ANTHROPIC_API_KEY` in the dashboard. Anywhere that runs a Python
web service works the same way:

- build: `pip install -r requirements.txt`
- start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

## How to use it

**One label.** Type in what the application says, add the label image, press
the button. Each field comes back as matching, needs checking, not matching, or
not found on the label, with the application value and the label value side by
side.

**Many at once.** Upload a CSV of applications plus the label images. The CSV
needs a `filename` column naming the image for each row, plus any of
`brand_name`, `class_type`, `abv`, `net_contents`, `producer`,
`country_of_origin`. Rows without an image and images without a row are both
reported rather than silently dropped. `sample_applications.csv` shows the
shape.

## Approach

The written brief left the technical requirements open, so the requirements
were taken from the four discovery interviews. What drove the design:

| From the interviews | What it became |
| --- | --- |
| Sarah: results in about 5 seconds or nobody uses it | One model call per label, transcription only, capped output |
| Sarah: importers dump 200 to 300 applications at once | Batch mode, CSV plus images, 8 concurrent |
| Sarah: her 73 year old mother should manage it | One screen, one action, plain words, no jargon |
| Dave: STONE'S THROW versus Stone's Throw is the same brand | Identity fields normalised before comparison |
| Dave: label review needs judgment | The tool reports findings and an agent can accept any of them. It never rejects anything |
| Jenny: the warning has to be exact, in capitals | The warning is compared strictly, against the 27 CFR 16.21 text |
| Jenny: labels are photographed badly | The extractor reports legibility, which is surfaced rather than hidden |
| Marcus: standalone proof of concept, nothing sensitive stored | No database, no file storage, images discarded after each request |

### The split between the model and the rules

The model only transcribes. It is told to report what is printed and not to
correct spelling, capitalisation or abbreviations. Everything that decides
whether a label passes lives in `app/matching.py` as ordinary Python.

This matters for three reasons. The compliance rules stay readable and
testable by people who are not engineers. The same input always produces the
same verdict. And the model call stays short, which is what keeps the round
trip inside Sarah's five second budget.

### Lenient on identity, strict on the warning

Brand name, class or type, and producer are casefolded, stripped of
punctuation and common company suffixes, then compared by similarity ratio.
At or above 0.90 the difference is treated as cosmetic. Between 0.75 and 0.90
it is flagged for a person. Below that it is a mismatch. So Dave's
`STONE'S THROW` passes, while `Whisky` against `Whiskey` gets a human look
rather than an automatic rejection.

The government warning gets the opposite treatment. The wording is compared
against the statutory text, and the `GOVERNMENT WARNING:` prefix is checked
separately for capitalisation so that Jenny's title case example is reported as
a capitalisation fault rather than as a vague wording difference.

Alcohol content is parsed to a number, so `45%` matches `45% Alc./Vol.
(90 Proof)`. A label whose stated proof contradicts its own percentage is
flagged even when the percentage matches the application. Net contents are
converted to millilitres, so `1 L` and `1000 mL` agree, with one percent
tolerance for metric and imperial rounding.

## Tools used

FastAPI and uvicorn, the Anthropic API for transcription, and one static HTML
page with no build step. The page is served from the same process as the API,
so there is one deployment and no cross origin configuration. The only
dependency in the comparison logic is the standard library.

## Assumptions

1. The application data arrives as typed fields or a CSV. The brief said not to
   integrate with COLA, so there is no import path from it.
2. The label is one image. Front and back labels submitted as separate files
   would each be checked separately.
3. Distilled spirits, wine and beer share one field set here. In practice the
   required fields differ by commodity, which is noted under limitations.
4. Country of origin is only checked when the application supplies it, since it
   applies to imports.
5. English language labels.
6. Net contents are volumetric. Standard of fill, which restricts containers to
   an approved list of sizes, is not checked.
7. Nothing needs to persist. There is no audit trail, which a real system would
   require.

## Limitations and trade-offs

This was built against a few hours. What was deliberately left out:

- **Poor image handling.** Jenny raised photographs taken at an angle, with
  glare or bad lighting. The extractor reports its own legibility and the
  result shows it, but there is no deskewing, no contrast correction and no
  retry at higher resolution. That is the first thing worth adding.
- **Commodity specific rules.** A real reviewer applies different required
  fields to beer, wine and spirits, plus type specific rules such as vintage
  and appellation for wine. One shared field set stands in for that here.
- **Typography checks.** The regulation sets minimum type sizes and requires
  the warning to be readily legible and separate from other text. Those are
  measurements on the image, not text comparisons, and are not implemented.
  Only the capitalisation of the prefix is checked.
- **The network constraint from Marcus's interview.** He said their firewall
  blocks outbound traffic to cloud ML endpoints, and that this is part of what
  killed the scanning vendor pilot. This prototype calls a hosted model, so as
  built it would not run inside that network. That is a deliberate choice for a
  standalone proof of concept and it is the first thing that would have to
  change for anything real. The path there is already open: transcription is
  one function behind one interface in `app/extract.py`, so swapping in an
  Azure OpenAI deployment inside the FedRAMP boundary, Azure AI Document
  Intelligence, or a self hosted OCR model means replacing that function. The
  comparison rules, the tests and the interface do not change.
- **No authentication, no audit log, no retention policy.** Marcus asked only
  that nothing crazy happen with a prototype, so nothing is stored at all.
  Production would need all three.
- **Tests cover the comparison rules, not the extraction.** The rules are
  deterministic and worth pinning down. Extraction accuracy would need a
  labelled set of real applications to measure properly, which is the
  evaluation a procurement decision should rest on rather than a demo.

## Performance

One label is one model call with a capped response, and the comparison work
after it is microseconds. Batches run eight at a time, so throughput scales
with concurrency while each individual label stays within the same budget.
Measured round trip is reported in the interface for every check, single or
batch, so the five second target is visible to the agent rather than asserted
in a document.

_Replace this line with the timings you measure on your own deployment._

## Layout

```
app/
  main.py          HTTP endpoints, batch orchestration, CSV parsing
  extract.py       the one model call, prompt, JSON parsing
  matching.py      all compliance rules, no model involved
  static/
    index.html     the whole interface
tests/
  test_matching.py the rules, including the cases raised in the interviews
sample_applications.csv
render.yaml
```
