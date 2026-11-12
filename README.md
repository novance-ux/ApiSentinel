# apitest

`apitest` generates and runs tests for an HTTP API from its OpenAPI 3.x specification. It helps check whether a live API follows its documented contract, handles invalid and boundary inputs safely, and supports common authentication and multi-step workflows.

## What it does

- Reads local or remote OpenAPI specifications, resolves references, and lists the documented endpoints.
- Generates valid request data from schemas and checks responses against documented status codes, content types, and response schemas.
- Probes security behavior, invalid inputs, and boundary values.
- Follows OpenAPI links and endpoint relationships for stateful create/read/update/delete workflows.
- Can optionally use Anthropic to suggest additional realistic test cases from endpoint descriptions and schemas.
- Reports failures with request details and `curl` reproducers, with credentials redacted by default.
- Writes reports in terminal, HTML, JSON, and JUnit XML formats.

Runs are repeatable with a fixed seed. The tool sends requests to the API you configure, so use a test or staging environment where you have permission to run generated requests. Stateful tests may create and clean up resources according to the configuration.

## Requirements

- Python 3.11 or newer
- An OpenAPI 3.x specification
- A reachable API target for `run`
- Anthropic credentials only for the optional `--llm` strategy

## Install from a source checkout

Create and activate a virtual environment:

```bash
python -m venv .venv
```

```bash
# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Install the runtime dependencies and run the CLI from the repository root:

```bash
python -m pip install "httpx>=0.27" "jinja2>=3.1" "jsonschema[format-nongpl]>=4.22" "openapi-spec-validator>=0.7" "pydantic>=2.7" "pyyaml>=6.0" "referencing>=0.35" "rich>=13.7" "typer>=0.12"
python -m apitest --help
```

The optional LLM case generator requires the Anthropic SDK and an API key:

```bash
python -m pip install "anthropic>=1.0"
```

Set `ANTHROPIC_API_KEY` in your environment before using `--llm`. LLM-generated cases send the endpoint schemas and descriptions to Anthropic; API responses and credentials are not sent.

## Quick start

### 1. Inspect a specification

Use the included sample or replace it with your own OpenAPI file or URL:

```bash
python -m apitest inspect examples/specs/buggy_api.yaml
```

Print the normalized endpoints as JSON or require strict OpenAPI validation:

```bash
python -m apitest inspect examples/specs/buggy_api.yaml --json
python -m apitest inspect examples/specs/buggy_api.yaml --strict
```

### 2. Start the included sample API

The included inventory API is deliberately buggy. Install its demo server dependencies:

```bash
python -m pip install fastapi uvicorn
```

In one terminal, from the repository root, start the API:

```bash
python -m uvicorn examples.buggy_api.app:app --host 127.0.0.1 --port 8000
```

### 3. Run tests against it

In a second terminal, from the repository root:

```bash
# macOS / Linux
export BUGGY_API_KEY=secret-key

# Windows PowerShell
$env:BUGGY_API_KEY = "secret-key"
```

Then run the default positive and security strategies:

```bash
python -m apitest run examples/specs/buggy_api.yaml --config examples/buggy_api/apitest.yaml
```

This demo is expected to find failures because the sample API intentionally violates its specification. Findings normally produce exit code `1`, which makes the command suitable for CI. Use `--exit-zero` when you want to collect a report without failing the command on findings.

## Test strategies

The default strategies are `positive` and `security`. Add strategies with repeatable `--strategy` / `-s` options:

| Strategy | What it exercises |
|---|---|
| `positive` | Schema-valid requests and documented success behavior |
| `security` | Requests with authentication withheld, where the specification requires it |
| `negative` | Invalid values and malformed inputs that should be rejected |
| `boundary` | Values at and around schema limits |
| `stateful` | Related operations such as creating a resource and then reading or updating it |
| `llm` | Additional model-suggested cases; requires the Anthropic extra and credentials |

For a broader demo run:

```bash
python -m apitest run examples/specs/buggy_api.yaml \
  --config examples/buggy_api/apitest.yaml \
  --strategy positive --strategy security --strategy negative \
  --strategy boundary --strategy stateful \
  --seed 42 --format html --format json --format junit --output reports
```

The same seed makes generated cases repeatable. Tune request concurrency, timeout, retries, rate limit, and the number of cases with CLI options or the YAML configuration file.

## Configuration

The optional `apitest.yaml` config file sets a target URL, authentication, request behavior, and strategies. See [examples/buggy_api/apitest.yaml](examples/buggy_api/apitest.yaml) for a complete example:

```yaml
base_url: https://api.example.test
auth:
  type: bearer
  token: ${API_TOKEN}
concurrency: 8
timeout: 10
retries: 2
rate_limit: 20
latency_threshold_ms: 1000
cases_per_endpoint: 3
seed: 0
strategies: [positive, security, negative, boundary]
redact_secrets: true
```

Values such as `${API_TOKEN}` are read from environment variables. Keep credentials out of committed YAML files. The `--base-url` option can override the configured URL. The runner contacts only the configured origin, rather than following a different origin advertised by the specification.

## CLI reference

```bash
python -m apitest --help
python -m apitest run --help
```

Common commands and options:

```bash
# Inspect a spec
python -m apitest inspect openapi.yaml

# Run against an API and write an HTML report
python -m apitest run openapi.yaml --base-url http://127.0.0.1:8000 --format html

# Run multiple strategies and write all report formats to a directory
python -m apitest run openapi.yaml --base-url http://127.0.0.1:8000 \
  -s positive -s security -s negative -s boundary -s stateful \
  -f html -f json -f junit -o reports

# Quiet terminal output, or suppress curl examples in terminal output
python -m apitest run openapi.yaml --base-url http://127.0.0.1:8000 --quiet
python -m apitest run openapi.yaml --base-url http://127.0.0.1:8000 --no-curl
```

If one report format is selected, `--output` is a file path. If multiple formats are selected, it is an output directory. Without `--output`, reports use the default `apitest-report` name in the current directory (or `reports/` for multiple formats).

## Project layout

```text
apitest/                  CLI, spec loading, test generation, execution, validation, reports
docs/                     Evaluation notes
examples/buggy_api/       Deliberately buggy FastAPI target and its configuration
examples/specs/           Example OpenAPI specifications
examples/ci/              GitHub Actions integration example
scripts/                  Evaluation scripts
tests/                    Automated test suite
dist/                     Local build artifacts (not tracked in Git)
```

## Development

Run the automated tests from the repository root after installing the runtime dependencies and `pytest`:

```bash
python -m pip install pytest
python -m pytest
```

The included API bug list and evaluation notes are in [examples/buggy_api/BUGS.md](examples/buggy_api/BUGS.md) and [docs/evaluation.md](docs/evaluation.md).
