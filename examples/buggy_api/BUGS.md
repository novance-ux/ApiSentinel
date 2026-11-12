# Injected bugs: ground truth

`examples/buggy_api/app.py` violates its contract (`examples/specs/buggy_api.yaml`) in
exactly the ways listed below. Each bug is tagged in the source with a matching
`# BUG-NN` comment, and `tests/test_buggy_api.py` asserts that every bug is present.

Failure categories follow apitest's classifier:
**contract** (contract violation), **5xx** (server error), **undoc** (undocumented
response), **perf** (performance), **security** (security smell).

| ID | Endpoint | Category | Trigger | Spec says | Actually happens | Found by (design intent) |
|----|----------|----------|---------|-----------|------------------|--------------------------|
| BUG-01 | `GET /items` | contract | any page | page holds `page_size` items | slice end off by one, so each page is one item short | stateful (listing check) |
| BUG-02 | `POST /items` | 5xx | `name: ""` | `minLength: 1` → 422 | `IndexError` building the SKU → 500 | boundary / fuzz (empty string) |
| BUG-03 | `POST /items`, `PUT /items/{id}` | contract | `price < 0` | `minimum: 0` → 422 | accepted (201/200) | boundary (min-1) |
| BUG-04 | `GET /items/{id}` | contract | any existing item | `created_at` is required | field missing from response | stateful (read after create) |
| BUG-05 | `GET /search` | undoc | `len(q)` in 51..200 | 200 or 422 | **418 I'm a teapot** | boundary (maxLength) / fuzz (long string) |
| BUG-06 | `GET /users/{id}` | 5xx + security | non-integer id | 422 | 500 with a Python traceback in the body | negative (wrong type) / fuzz |
| BUG-07 | `DELETE /items/{id}` | security | no `X-API-Key` | 401 | request processed (204/404) | security probe / stateful |
| BUG-08 | `PUT /items/{id}` | contract | any update | `price` is a number | `price` returned as a string | stateful (update) |
| BUG-09 | `POST /users` | undoc | any valid user | 201 | 200 | positive |
| BUG-10 | `GET /items/{id}` | contract | read after DELETE | 404 | 200 (soft delete ignored) | stateful (read after delete) |
| BUG-11 | `POST /items`, `PUT /items/{id}` | contract | `quantity: 10000` | inclusive maximum → accepted | 422 (off by one in validation) | boundary (max) |
| BUG-12 | `POST /items`, `PUT /items/{id}` | 5xx | `description: null` | `nullable: true` → accepted | `AttributeError` → 500 | negative / fuzz (null) |
| BUG-13 | `GET /search` | 5xx | non-ASCII `q` (e.g. `café`, emoji) | 200 | `UnicodeEncodeError` → 500 | fuzz (unicode) |
| BUG-14 | `POST /users` | contract + security | unknown field, e.g. `is_admin: true` | `additionalProperties: false` → 422 | stored and echoed back (mass assignment) | negative (unknown field) |
| BUG-15 | `GET /items` | perf | `page_size > 50` | fast | +1.2 s latency | positive / boundary |
| BUG-16 | `POST /items`, `PUT /items/{id}` | 5xx | `metadata` nested deeper than 32 | any depth accepted | undocumented internal limit → 500 | fuzz (deep nesting) |
| BUG-17 | `GET /health` | contract | always | `application/json` | `text/plain` | positive |
| BUG-18 | `POST /orders` | 5xx | `quantity: 0` | `minimum: 1` → 422 | passes validation, `ZeroDivisionError` → 500 | boundary (min-1) |
| BUG-19 | `POST /items` | 5xx | `name` containing `{` or `}` | 201 | name concatenated into a `str.format` template → 500 | fuzz (format strings) |

## Machine-readable matchers

The evaluation script (`scripts/evaluate.py`) parses the YAML block below and uses it to
attribute each reported failure to a bug. A failure matches a bug when it matches
**any** entry in the bug's `match` list. An entry matches when **all** of its given
keys hold:

- `endpoint`: `"METHOD /path"` or a list of them, compared to the failure's endpoint
- `checks`: the failure's check name is one of these
- `status`: the observed response status is one of these
- `message`: a regex that must match the failure message (for symptoms visible only in
  the response, such as a bad value read back from another endpoint)
- `request`: every condition holds for the request that triggered the failure.
  `field` is `body.<dotted.path>`, `query.<name>`, or `path.<name>`. `op` is one of
  `equals`, `is_null`, `lt`, `gt`, `len_gt`, `depth_gt`, `non_ascii`, `regex`, `not_int`.

Failures that match no bug are counted as false positives. Review them by hand: a
failure that matches no bug may be a real bug that nobody injected on purpose.

```yaml
bugs:
  - id: BUG-01
    match:
      - endpoint: GET /items
        checks: [pagination_short_page, created_resource_missing]
  - id: BUG-02
    match:
      - endpoint: POST /items
        checks: [server_error]
        request: [{ field: body.name, op: equals, value: "" }]
      - endpoint: PUT /items/{item_id}
        checks: [invalid_input_accepted]
        request: [{ field: body.name, op: equals, value: "" }]
  - id: BUG-03
    match:
      - endpoint: ["POST /items", "PUT /items/{item_id}"]
        checks: [invalid_input_accepted, schema_mismatch]
        request: [{ field: body.price, op: lt, value: 0 }]
      # secondary symptom: a stored negative price read back through another endpoint
      - endpoint: [GET /items, GET /search, "GET /items/{item_id}"]
        checks: [schema_mismatch]
        message: "price: -[0-9.]+ is less than the minimum of 0"
  - id: BUG-04
    match:
      - endpoint: GET /items/{item_id}
        checks: [schema_mismatch]
  - id: BUG-05
    match:
      - endpoint: GET /search
        checks: [status_undocumented, valid_input_rejected]
        status: [418]
  - id: BUG-06
    match:
      - endpoint: GET /users/{user_id}
        checks: [server_error, stack_trace_leak]
  - id: BUG-07
    match:
      - endpoint: DELETE /items/{item_id}
        checks: [auth_not_enforced]
  - id: BUG-08
    match:
      - endpoint: PUT /items/{item_id}
        checks: [schema_mismatch]
  - id: BUG-09
    match:
      - endpoint: POST /users
        checks: [status_undocumented]
        status: [200]
  - id: BUG-10
    match:
      - endpoint: GET /items/{item_id}
        checks: [deleted_resource_retrievable]
  - id: BUG-11
    match:
      - endpoint: ["POST /items", "PUT /items/{item_id}"]
        checks: [valid_input_rejected]
        request: [{ field: body.quantity, op: equals, value: 10000 }]
  - id: BUG-12
    match:
      - endpoint: ["POST /items", "PUT /items/{item_id}"]
        checks: [server_error]
        request: [{ field: body.description, op: is_null }]
  - id: BUG-13
    match:
      - endpoint: GET /search
        checks: [server_error]
        request: [{ field: query.q, op: non_ascii }]
  - id: BUG-14
    match:
      - endpoint: POST /users
        checks: [invalid_input_accepted, schema_mismatch]
  - id: BUG-15
    match:
      - endpoint: GET /items
        checks: [latency]
  - id: BUG-16
    match:
      - endpoint: ["POST /items", "PUT /items/{item_id}"]
        checks: [server_error]
        request: [{ field: body.metadata, op: depth_gt, value: 32 }]
  - id: BUG-17
    match:
      - endpoint: GET /health
        checks: [content_type_mismatch]
  - id: BUG-18
    match:
      - endpoint: POST /orders
        checks: [server_error]
        request: [{ field: body.quantity, op: equals, value: 0 }]
  - id: BUG-19
    match:
      - endpoint: POST /items
        checks: [server_error]
        request: [{ field: body.name, op: regex, value: "[{}]" }]
```
