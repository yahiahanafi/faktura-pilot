# Fakturama Image-to-Cash

This project extracts a purchase order from one image, validates the source values, and automates an Order-first flow in Fakturama. It is designed for Fakturama 2.2.0 on Windows. The source image is sent to the OpenAI API for extraction; subsequent document work happens in the local Fakturama desktop application.

## Architecture

```text
Order image
    -> GPT-6 Luna structured extraction
    -> Pydantic domain validation and Decimal total checks
    -> deterministic workflow policy
    -> Fakturama UIA controls, with OCR fallback for visible text
    -> saved Order -> linked Invoice -> persisted-state verification
```

The extraction adapter returns the canonical `OrderSource` model. Business branching is deterministic: the model reads the image but does not choose master records or decide whether to create them. The workflow orchestrator coordinates an injected Fakturama gateway. The UIA resolver locates live controls from their names, types, relationships, and current UI state; OCR is a fallback for visible text that UIA does not expose. It does not depend on fixed screen coordinates.

The workflow orchestrator keeps the New Order open while it resolves the Debtor and Products. It selects an existing record only when the configured identity fields match exactly, creates a record only when no exact result exists, and stops for manual review when results are ambiguous or a verification check fails. Saved records are re-read from Fakturama before the next document action. Real desktop execution requires the Windows gateway to be available and connected to a disposable Fakturama workspace.

## Prerequisites

- Windows 10 or 11 with an interactive desktop session. Fakturama UI automation cannot run in a headless GitHub Actions worker.
- Python 3.12.
- Fakturama 2.2.0 configured in English. Use a disposable Fakturama workspace while developing: the flow can create Debtors, payment terms, VAT rates, Products, Orders, and Invoices.
- An OpenAI API key for image extraction. The image is sent to the OpenAI API; the request uses `store=False`.
- For OCR fallback, install the Tesseract executable separately and make `tesseract.exe` available on `PATH`.

## Install

From the repository root in PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[automation,ocr,dev]"
```

The `automation` extra installs Windows UI Automation support. The `ocr` extra installs Python OCR support and optional OpenCV preprocessing; Tesseract itself remains a separate system installation. Both extras are optional so extraction, domain validation, unit tests, and CI do not need Windows UIA or Tesseract.

Set the API key in the current PowerShell session. The prompt hides the value, and the variable is not written to the repository:

```powershell
$credential = Get-Credential -UserName "OpenAI API key" -Message "Enter the key as the password"
$env:OPENAI_API_KEY = $credential.GetNetworkCredential().Password
Remove-Variable credential
```

For repeatable noninteractive runs, set `OPENAI_API_KEY` in the process environment using your secret manager. Do not put the key in `examples/config.toml`, source control, screenshots, or run logs.

The checked-in [sample config](examples/config.toml) contains non-secret defaults. `OPENAI_API_KEY` is read from the environment and overrides file settings. `IMAGE_TO_CASH_MODEL`, `IMAGE_TO_CASH_REASONING_EFFORT`, `IMAGE_TO_CASH_REQUEST_TIMEOUT_SECONDS`, `IMAGE_TO_CASH_MAX_IMAGE_BYTES`, and `IMAGE_TO_CASH_PROMPT_VERSION` can also override their TOML values. The model defaults to `gpt-6-luna` with `low` reasoning effort.

## CLI

After installation, run `image-to-cash --help` (or `python -m faktura_pilot --help`). The CLI supports `extract`, `validate`, `run`, `resume`, and `inspect`:

```powershell
# Extract an image and write the validated canonical order JSON.
image-to-cash extract `
  --image "C:\orders\order.png" `
  --config examples\config.toml `
  --output run-data\order.json

# Validate a previously extracted canonical JSON file without an API request.
image-to-cash validate --source-json examples\order-source.json

# Validate by extracting an image; this needs OPENAI_API_KEY.
image-to-cash validate --image "C:\orders\order.png" --config examples\config.toml

# Use a canned JSON fixture instead of the OpenAI API for offline CLI checks.
image-to-cash extract `
  --image "C:\orders\order.png" `
  --fixture-json examples\order-source.json `
  --output run-data\fixture-order.json

# Run the continuous Order-first flow with a real image and the OpenAI API.
# A verified run ends with a saved Order and linked Invoice.
image-to-cash run `
  --image "C:\orders\order.png" `
  --config examples\config.toml `
  --run-dir run-data

# Exercise the same Fakturama workflow with the canonical fixture instead of an API call.
# This still changes Fakturama; use a disposable workspace.
image-to-cash run `
  --image "C:\orders\order.png" `
  --fixture-json examples\order-source.json `
  --run-dir run-data `
  --run-id sample-order

# Inspect a run without opening or changing Fakturama.
image-to-cash inspect --run-id sample-order --run-dir run-data

# Resume only after resolving the review item shown by inspect/review.json.
image-to-cash resume --run-id sample-order --run-dir run-data
```

`extract --image` writes the validated canonical JSON. `validate --source-json` checks a canonical JSON file without an API request; `validate --image` extracts and validates but does not create Fakturama records. The fixture extractor ignores the required `--image` argument and returns the fixture, which makes extraction tests offline. For `run`, however, the Windows UIA gateway is still real and can create records. Omit `--fixture-json` to use OpenAI extraction. `resume` reads the original validated source and pending action from the saved checkpoint; it does not call the extractor again. `inspect` is read-only. `run` and `resume` return exit code 3 when the workflow pauses for manual review.

`run` accepts `--run-dir` (alias `--runs-dir`, default `run-data`), optional `--run-id`, `--evidence-dir`, `--config`, `--fixture-json`, and `--fakturama-exe`. `resume` accepts the run directory, optional evidence directory, and optional executable path. The per-run folder stores the checkpoint, event log, review bundle when paused, and evidence images when captured. Keep run data private: it includes extracted customer and order data.

## Deterministic rules and validation

- Monetary amounts use `Decimal`; line and VAT calculations round to two places with `ROUND_HALF_UP`. Extracted line and order totals must agree with calculated totals within EUR 0.01.
- A Debtor is an exact match only when company, first name, last name, ZIP code, and city match after Unicode compatibility normalization, whitespace normalization, and case folding. No fuzzy match is used.
- Product SKU matching trims surrounding whitespace and remains case-sensitive.
- VAT reuse requires the exact `VAT <rate>%` name, the same percentage value, and E-Invoice code `S` (Standard rate).
- Payment method mapping is explicit: Bank Transfer → Credit transfer; Credit Card → Credit card; SEPA Direct Debit → SEPA direct debit. Unsupported or ambiguous methods require review.
- A `PAID` source requires an explicit payment date. `UNPAID` sources cannot contain a payment date; the automation must not invent a paid date or amount.
- The source customer ID is retained for audit. It does not replace Fakturama's proposed Customer ID.
- When creating master data, the Debtor gets zero discount and Net price mode; new payment terms use zero cash discount and day values and are not made the default. Product VAT must match the source line and use the `S` Standard-rate code.

Manual review is the safe outcome when the UI presents multiple exact matches, an existing VAT definition conflicts, an expected value cannot be read back, or totals do not agree. Correct the source data or Fakturama record, then resume only from a verified checkpoint where supported.

## Evidence and screenshots

The workflow stores `checkpoint.json` and `events.jsonl` per run, plus `review.json` when it pauses for review. A review bundle includes the failed step, reason, expected and observed values, candidates, and a screenshot path when capture succeeds. The gateway captures evidence on verification failures and when the workflow pauses; it does not automatically save a screenshot for every successful checkpoint. Reviewers should inspect the canonical JSON, event history, screenshots, and Fakturama Documents list together; a screenshot alone is not proof that a record was saved.

For the assessment walkthrough, prepare a disposable Fakturama workspace, run the sample order, and capture the Order editor before save, the saved Order row, the linked Invoice with payment fields, and the final Documents list showing the Invoice and still-open Order. Mask any real customer data or API key before sharing evidence. The supplied example JSON is synthetic.

## Tests and static checks

```powershell
python -m unittest discover -s tests -v
ruff check src tests
python -m compileall -q src tests
```

The GitHub Actions workflow installs the project and development tools, then runs `ruff check src tests` and `python -m pytest` under Python 3.12 without Fakturama, UIA, OCR, or API credentials. The tests can also be run locally with `python -m unittest discover -s tests -v`; Ruff and pytest are part of the `dev` extra. The Fakturama desktop flow must be exercised locally in a disposable workspace.

## Known limitations and skipped work

- Part 1, the separate design-document deliverable, is intentionally skipped.
- The OpenAI adapter is covered with a mocked response in unit tests; a live API extraction has not been run as part of automated CI.
- The Windows UIA/OCR gateway and end-to-end CLI are implemented, but live Fakturama UI behavior has not been smoke-tested in this environment. The gateway must still be validated against the exact Fakturama 2.2.0 installation, English UI labels, Windows scaling, and workspace data.
- GitHub Actions cannot verify Windows UIA behavior or Fakturama document persistence. Those checks require an interactive Windows desktop and a disposable Fakturama 2.2.0 workspace.
- OCR support depends on a separately installed Tesseract binary. OpenCV is optional; when unavailable, the OCR path uses grayscale preprocessing.
- The assessment flow accepts EUR and the English Fakturama UI only. Other currencies, unsupported payment mappings, missing required source fields, unreadable VAT, and ambiguous master records pause for review rather than being guessed.
- Successful-step screenshots and a short recording must be captured manually during a local end-to-end run; CI does not fabricate UI evidence. Failure/review screenshots are captured when possible.
- The Debtor and Product forms use semantic labels discovered from UI Automation. A Fakturama build with materially different or inaccessible labels can require a manual review instead of continuing.

## If I had 3 more hours

I would run the full supplied-image case against a disposable Fakturama 2.2.0 workspace and validate the persisted Order/Invoice relationship, payment state, and master data. I would also exercise recovery after a forced pause, correct any UIA/OCR label mismatches found on the real installation, and capture the required annotated screenshots and short recording. Unit and fake-gateway tests cannot establish those live desktop behaviors.
