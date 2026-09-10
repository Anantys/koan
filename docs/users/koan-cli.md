---
type: doc
title: "Kōan REST CLI"
description: "Configure and use bin/koan-cli to call every operation in Kōan's token-authenticated REST API, with self-documenting --help."
tags: [users]
created: 2026-09-08
updated: 2026-09-09
---

# Kōan REST CLI

`bin/koan-cli` reads `koan/openapi.yaml` at runtime and exposes every
documented REST operation without requiring package installation. Enable and
start the API first, as described in the [REST API guide](../operations/rest-api.md).

## Configure

Create or update the default profile interactively:

```bash
bin/koan-cli configure
```

Profiles are stored in `~/.config/koan-cli.cfg`. The CLI creates this file with
mode `0600` and also accepts an existing mode-`0400` file. It refuses a
group- or world-readable file and prints the exact `chmod 600` repair command.
Tokens never belong in command-line arguments.

Create or select another profile by putting global options before the command:

```bash
bin/koan-cli --profile prod configure
bin/koan-cli --profile prod status
```

Settings resolve in this order:

1. `--profile`, `--base-url`, and `--timeout` command-line options.
2. Non-empty `KOAN_PROFILE`, `KOAN_BASE_URL`, `KOAN_API_TOKEN`, and
   `KOAN_TIMEOUT` values.
3. Values in the selected profile (`base_url`, `token`, `timeout`).
4. The first server URL in `koan/openapi.yaml` for the base URL, and a
   120-second response timeout.

`configure` uses the same ladder to pick which profile it writes and which
base URL it offers as the prompt default, so `KOAN_PROFILE=prod koan-cli
configure` writes `[prod]`, not `[default]`.

Empty environment values are ignored. Without any configuration, the public
`health` command uses the specification's default local server and needs no
token:

```bash
bin/koan-cli health
```

Configuration always saves before verification. Verification checks public
health for reachability, then authenticated status to confirm the token; a
stopped server does not discard the saved profile. Failed verification returns
the corresponding nonzero exit code.

## Generated commands

Commands follow the API resources. `--help` is generated from the OpenAPI
document, so every root, group, and leaf carries a one-line description pulled
from its view's summary, and every spec-described flag shows its help text:

```bash
bin/koan-cli --help              # every root command, plus global examples
bin/koan-cli missions --help     # every leaf, plus group examples
bin/koan-cli missions create --help  # per-flag help and the command XOR text note
bin/koan-cli missions list -q status=pending
bin/koan-cli missions get MISSION_ID
bin/koan-cli observability logs
```

Because the help is derived from the spec, a new endpoint is documented in the
CLI for free as soon as its view docstring and `koan/openapi.yaml` land. Bodies
declared with `anyOf` (for example `POST /v1/missions`, where `command` and
`text` are mutually exclusive) render that constraint in prose on the command's
`--help`.

Each OpenAPI `operationId` also works as a hidden root-level alias for scripts
that prefer specification identifiers. Public command names and aliases are
validated at startup so ambiguous specification changes fail locally.

Most command names come from the path, not the method. When one path exposes
two methods that would otherwise share a name, each gains a `-<method>` suffix
(`admin pause-get`, `admin pause-post`) instead of aborting the whole client.
Collection and item paths keep their readable names: `missions list`,
`missions create`, `missions get`, `missions update`, `missions delete`.

## Response timeout

`--timeout SECONDS` bounds how long the client waits for a response. The
default is 120 seconds because several handlers do their work synchronously
inside the request — `admin update` runs `git fetch` plus `git pull`, and
`projects create` clones a repository.

```bash
bin/koan-cli --timeout 600 admin update --yes
KOAN_TIMEOUT=600 bin/koan-cli admin update --yes
```

`configure` does not write a timeout. Add `timeout = 600` under a profile
section by hand to make a larger budget the default for that profile.

A timeout is reported distinctly from a failed request, because the server may
already have applied a non-idempotent operation:

```
no response within 120s: HTTPConnectionPool(...): Read timed out.
POST http://127.0.0.1:8420/v1/update may still have been applied; verify before
retrying, or raise --timeout.
```

Check the outcome before retrying such a command.

## Generic input

Every operation accepts JSON through `--data`, from either inline text or an
`@`-prefixed file, plus repeatable query pairs through `-q` or `--query`. This
includes GET requests.

```bash
bin/koan-cli missions create --data '{"command":"/review https://github.com/org/repo/pull/42"}'
bin/koan-cli missions create --data @request.json
bin/koan-cli missions list -q status=pending -q project=my-toolkit
```

Duplicate generic query keys use the last value. Schema-derived typed flags
appear automatically when the OpenAPI document contains request or query
schemas, and will override a generic query value with the same name. The typed
flags use clean metavars (`COMMAND`, `TEXT`, `PROJECT`, …) and their `--help`
shows the schema's description where the spec provides one.

A flag whose schema is an `object` or `array` takes JSON (metavar `JSON`) and
is parsed before dispatch, so a structured field arrives as structure rather
than as a quoted string. Invalid JSON, or JSON of the wrong shape, fails
locally without sending a request:

```bash
bin/koan-cli projects update my-toolkit --patch '{"focus": true}'
```

Path parameters are positional and percent-encoded before dispatch:

```bash
bin/koan-cli projects update my-toolkit --data '{"patch":{"focus":false}}'
```

## Raw requests

Use `raw` for debugging or for a server endpoint newer than the checked-out
specification:

```bash
bin/koan-cli raw GET /v1/status
bin/koan-cli raw POST /v1/missions --data @request.json -q trace=test
```

Raw requests require authentication except exactly `GET /v1/health`. A raw
path must begin with `/`.

## Output and exit codes

Successful responses go to stdout as valid JSON. Output is pretty-printed on a
TTY and compact when piped; `--pretty` and `--compact` override detection.
HTTP error bodies and local diagnostics go to stderr, leaving stdout empty.
Non-JSON response bodies are wrapped in a JSON object.

| Code | Meaning |
|---|---|
| `0` | HTTP success |
| `1` | Local/usage error or other 4xx response |
| `2` | Authentication failure (`401` or `403`) |
| `3` | Not found (`404`) |
| `4` | Server error (`5xx`) |

All DELETE requests plus restart, shutdown, update, and release-update requests
are destructive. They prompt when stdin is a TTY and require `--yes` in
non-interactive scripts.

```bash
bin/koan-cli missions delete MISSION_ID --yes
```

## Troubleshooting

- `authentication required` means the selected profile has no token. Run
  `bin/koan-cli configure` or set `KOAN_API_TOKEN`.
- `token was rejected` means health succeeded but authenticated status returned
  `401` or `403`. Generate a matching token with `make api-token`.
- A connection-refused diagnostic lists the API setup sequence: enable
  `api.enabled`, configure a generated token, then run `make api`.
- Invalid JSON, malformed query pairs, and missing required values fail locally
  without sending a request.
- `no response within Ns` means the request was delivered but no reply arrived.
  Verify the server-side outcome, then retry with a larger `--timeout`.
- `saved; cannot reach <url>: <error>` names the transport failure, so a
  malformed URL or a TLS problem is distinguishable from a stopped server.
