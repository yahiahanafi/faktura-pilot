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

The Order-first flow opens the New Order editor from **Order**, leaves its proposed **No.** unchanged, and sets the extracted **Date** and **Cust.Ref.**. It uses **Net** price mode with **VAT** set to **With VAT**. The Order's Debtor and Product selectors are used for exact-match lookups first. The Order stays open while any missing Debtor, payment method, VAT rate, or Product is created through the corresponding Fakturama data view, then the new record is selected from the same Order.

## Prerequisites

- Windows 10 or 11 with an interactive desktop session. Fakturama UI automation cannot run in a headless GitHub Actions worker.
- Python 3.12.
- Fakturama 2.2.0 configured in English. Use a disposable Fakturama workspace while developing: the flow can create Debtors, payment terms, VAT rates, Products, Orders, and Invoices.
- An OpenAI API key for image extraction. The image is sent to the OpenAI API; the request uses `store=False`.
- OCR is optional for the UIA workflow. For OCR fallback, install Tesseract separately. The gateway checks `PATH`, the `FAKTURA_PILOT_TESSERACT_EXE` override, and common Windows installation locations.

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

After installation, run `image-to-cash --help` (or `python -m faktura_pilot --help`). The CLI supports `extract`, `validate`, `run`, `resume`, `inspect`, and the read-only `doctor` command:

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

# Diagnose the installed automation dependencies and currently open Fakturama window.
# This only reads the window and installation metadata; it does not launch or operate Fakturama.
image-to-cash doctor --fakturama-exe "C:\Program Files\Fakturama2\Fakturama.exe"

# Resume only after resolving the review item shown by inspect/review.json.
image-to-cash resume --run-id sample-order --run-dir run-data
```

`extract --image` writes the validated canonical JSON. `validate --source-json` checks a canonical JSON file without an API request; `validate --image` extracts and validates but does not create Fakturama records. The fixture extractor ignores the required `--image` argument and returns the fixture, which makes extraction tests offline. For `run`, however, the Windows UIA gateway is still real and can create records. Omit `--fixture-json` to use OpenAI extraction. `resume` reads the original validated source and pending action from the saved checkpoint; it does not call the extractor again. `inspect` is read-only. `run` and `resume` return exit code 3 when the workflow pauses for manual review.

`doctor` is read-only: it reports Python automation/OCR dependencies, executable discovery, the visible Fakturama window, its version and language, and the current process's DPI awareness. Missing or unusable Tesseract is a warning; UIA-based actions can remain available, while actions that need OCR stop with an explicit error. When connecting, the gateway checks visible Fakturama windows first and attaches to a single match by window handle. If no window is visible, it searches Windows App Paths and common install locations. Set `FAKTURAMA_EXE` or pass `--fakturama-exe` to select another path; multiple discovered installations require an explicit path. The gateway starts an executable only after a fresh window scan and process inventory confirm no matching Fakturama process is running. After startup, it rescans for a visible window for up to three minutes and reports an error if the process never exposes one.

`run` accepts `--run-dir` (alias `--runs-dir`, default `run-data`), optional `--run-id`, `--evidence-dir`, `--config`, `--fixture-json`, and `--fakturama-exe`. Omit `--fakturama-exe` to use automatic discovery, or set `FAKTURAMA_EXE` for an installation outside the searched locations. `resume` accepts the run directory, optional evidence directory, and optional executable path. The per-run folder stores the checkpoint, event log, review bundle when paused, and evidence images when captured. Keep run data private: it includes extracted customer and order data.

## Live progress and troubleshooting

`run`, `resume`, `extract`, and `validate` report their current step to the command window automatically. Each line includes local time and elapsed seconds. The report shows image extraction and independent verification, connection checks, customer/product resolution, actions awaiting confirmation, verified workflow states, and the reason for any review pause. During a blocking operation, a heartbeat reports the current step every 20 seconds without restarting or repeating that operation.

When `run` or `resume` stops for manual review in an interactive Windows terminal, it plays a warning sound and opens a popup showing the run ID, reason, and path to `review.json`. The popup appears after automation stops and the review bundle is saved. Use **Open review bundle** to inspect the saved details. After resolving the issue in Fakturama, click **Yes, continue**. The workflow verifies completed work, skips those items, and continues the same run in a new console; **Close** dismisses the popup. The original command returns exit code 3 without waiting for the popup. Use `--no-review-alert` to suppress it, or `--review-alert` to enable it explicitly when running with redirected input. `--quiet` also disables the alert by default. Noninteractive runs and non-Windows systems do not alert automatically. The popup helper remains open until dismissed; closing the command window does not dismiss it. When `run` or `resume` completes the full Order and Invoice workflow, it also plays a success sound and opens a popup with the run ID and document numbers. Completion popups follow the same interactive Windows defaults; use `--no-completion-alert` to suppress one or `--completion-alert` to enable it with redirected input.

Use `--progress-interval 5` for more frequent heartbeat checks or `--quiet` to suppress progress. Progress is flushed to **stderr**, keeping extracted JSON on **stdout** usable by other programs. To retain the console report in PowerShell:

```powershell
image-to-cash run --image examples\order-image.png `
  --fixture-json examples\order-source.json --run-id progress-demo `
  --run-dir run-data --progress-interval 5 2> run-progress.log
```

This example uses the validated fixture and still operates Fakturama. Omit `--fixture-json` to extract the image through the API. Use `resume` for an existing checkpoint; it reuses validated source data and avoids both extraction requests. The example config explicitly uses high reasoning effort, while the application default is low; preserve the verification pass when tuning extraction speed.

The durable `events.jsonl` now records step starts as well as confirmed actions and verified states, so interrupted runs show the last operation reached. Review details remain in `review.json`. A heartbeat means the process is still waiting; it does not claim that a UI action succeeded.

Automation work has been reduced by sharing a UIA tree snapshot within each field-resolution operation and reading selector labels and rows from one OCR pass. Product lookup previously used four OCR passes. Native window discovery also reduced connection and environment checks from 41.2 seconds to 1.0 second in the recorded live comparison; this is a startup measurement, not full-run timing. Snapshot reuse ends before the next UI action to avoid stale controls. Overlapping OCR copies of the same visible label are merged, while distinct matching controls still require review. These changes remove redundant work; end-to-end speed depends on the desktop and must be measured on a live run.

The [saved-run audit](docs/run-data-audit.md) records the cause and recovery disposition for every historical run. Leave those checkpoints intact as evidence. A run with an incorrect extracted postcode needs corrected source data in a new run. A pending action with an uncertain outcome must be reconciled against the existing editor or saved document before it can continue.

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

