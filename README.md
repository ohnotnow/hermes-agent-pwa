# hap

Talk to your local Hermes agents from your phone, like a simple messaging app.

hap (Hermes Agent Proxy) is a small, self-hosted gateway for messaging your own
[Hermes](https://hermes-agent.nousresearch.com/) agents from a phone, tablet or
browser. You pick an agent, send a message, and the reply turns up in the same
conversation, whether the agent answers in two seconds or two hours. It is
built for one person and a handful of agents. No third-party messaging
platforms sit in the middle, there is no message broker, and nobody else is
reading your threads.

## What it does

You open a private URL, log in, and chat with your agents. Each agent has its
own conversations, and history sticks around, so a thread is still there when
you reopen the app. New replies arrive live while the app is open (over
Server-Sent Events) and are waiting for you when it is closed. If an agent is
offline, hap tells you instead of leaving you to wonder. And if you hid a
conversation, a late reply quietly brings it back.

There are two pieces:

- A gateway: a FastAPI app with a SQLite store that serves the web app (an
  installable PWA) and a couple of endpoints for agents. SQLite is the durable
  source of truth, and there is no message broker.
- A Hermes plugin: a small platform adapter that runs alongside each Hermes
  agent, polls the gateway for your messages, and posts the agent's replies
  back. As far as the agent is concerned, hap is just another chat platform, so
  it needs no special knowledge of how hap works.

The browser only ever names an agent by id, never a raw address, and the agent
reaches the gateway with a bearer token.

(Yes, the repository is still named `nats-agent-gw`. An early design used NATS
as the message fabric, then I dropped it in favour of plain HTTP and SQLite,
which turned out to be plenty for one person and a few agents. The name just
never caught up.)

## Prerequisites

- Python 3.11 or newer, and [uv](https://docs.astral.sh/uv/) for dependencies.
- [Hermes](https://hermes-agent.nousresearch.com/) installed on each machine
  whose agents you want to reach.
- A way for your phone to reach the gateway: Tailscale (recommended), Caddy
  with a public domain, or the same local network.

## Getting started

The simplest case is a single machine that also runs Hermes, so that is what
these steps cover. Clone the repository, then from its directory:

```
./scripts/install.sh
```

The installer:

- installs the gateway's dependencies with uv,
- generates a bearer token (saved to `hap_token.txt`) and derives a six-digit
  login PIN from it,
- copies the plugin into `~/.hermes/plugins/hap`, writes its config, and enables
  it,
- prints the URL to open, the token, and the PIN.

A few options worth knowing: `--agent <name>` sets this box's agent id (default
`hermes`), `--host <addr>` sets the gateway bind address (default `127.0.0.1`),
and `--public-url <url>` sets the address printed for your phone. Run
`./scripts/install.sh --help` for the full list.

Then start the two pieces, in separate terminals or as services:

```
uv run uvicorn app.main:app --host 127.0.0.1 --port 8088   # the gateway
hermes gateway run                                         # Hermes + the plugin
```

Open the printed URL and log in with either the full token or the short PIN.

## Reaching it from your phone

The gateway binds to `127.0.0.1` by default, so your phone needs a way in.
Here are the options, easiest first:

- Tailscale (recommended), because it gives you HTTPS, which the installable PWA
  needs:

  ```
  tailscale serve --bg 8088
  ```

  then open `https://<your-machine>.<your-tailnet>.ts.net`. The first time, your
  tailnet may need Serve (and HTTPS) switched on, in which case Tailscale prints
  an admin console link to do it.
- Caddy, for a public domain with automatic HTTPS. See
  [`caddy/Caddyfile.example`](caddy/Caddyfile.example).
- Same LAN only. Re-run the installer with `--host 0.0.0.0` and use
  `http://<lan-ip>:8088`.

One catch to be aware of: "Add to Home Screen", offline support and the service
worker only work over HTTPS or on `localhost`, because browsers insist on a
secure context for service workers. Plain http to a LAN or Tailscale IP runs
fine as a web page, it just will not install as an app. If you want the full
installable PWA, Tailscale's HTTPS (above) is the easiest route to it.

## Configuration

The gateway reads a few environment variables, all optional:

- `HAP_AUTH_TOKEN`: the shared bearer token. If unset, it is read from
  `hap_token.txt` (or the file named by `HAP_TOKEN_FILE`).
- `HAP_HOST` / `HAP_PORT`: bind address and port (default `127.0.0.1:8088`).
- `HAP_COOKIE_SECURE`: set to `true` when serving over HTTPS (behind Caddy or
  Tailscale) so the session cookie is marked `Secure`. Left `false` for local
  http.
- `HAP_DB_PATH`: SQLite file path (default `hap.db`).

The plugin reads `~/.hermes/plugins/hap/hap.json`, which the installer writes.
See [`hermes_plugin/hap/hap.json.example`](hermes_plugin/hap/hap.json.example)
for the shape: the gateway URL it connects to, the bearer token, this agent's
id, and the poll interval.

## Running on Linux as a service

Running this on a Raspberry Pi or a VPS? [`systemd/hap-gateway.service`](systemd/hap-gateway.service)
is a starting point for running the gateway under systemd. Put Caddy or
Tailscale in front of it for HTTPS.

## Contributing

Contributions are welcome. To get set up:

```
git clone <repo-url>
cd nats-agent-gw
uv sync
```

Run the gateway with `uv run uvicorn app.main:app --reload` and open
http://127.0.0.1:8088.

## Licence

Released under the MIT Licence. See [LICENSE](LICENSE).
