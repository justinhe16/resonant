# linear extension

Linear for Resonant, through **Linear's hosted MCP server** (`https://mcp.linear.app/mcp`, streamable HTTP). There is no code here: the manifest lists the server tools Resonant may use, each with its effect, level and a description we wrote, and Resonant speaks MCP to that one URL with your Linear API key as the bearer token. See [`docs/extensions.md`](../../docs/extensions.md) §4, MCP tools, for what that guarantees.

| Tool | Level | What it does |
|---|---|---|
| `list_issues` | L0 | List or search issues by query text, team, project, state or assignee (`me` for yours). "What's on my Linear" lands here. |
| `get_issue` | L0 | One issue by id or key (`ENG-123`). |
| `list_comments` | L0 | The comments on one issue. |
| `list_projects` | L0 | Projects, optionally by team or query. |
| `list_teams` | L0 | Teams and their keys. |
| `save_issue` | L2 | Create an issue (no id: title and team) or update one (with its id). Asks a plain yes first. |
| `save_comment` | L2 | Add a comment to an issue (or edit one, with its id). Asks a plain yes first. |

The server offers more tools (documents, cycles, initiatives, …). Resonant never registers or calls a tool this manifest doesn't list; `ext add` shows them as a note.

## Auth

Linear's MCP docs (<https://linear.app/docs/mcp>) say the server accepts "OAuth token and API keys directly in the `Authorization: Bearer <yourtoken>` header", as an alternative to its interactive OAuth flow. Resonant has no OAuth flow for MCP (out of scope in [`dashboard-plan.md`](../../docs/dashboard-plan.md) §2), so this extension uses a **personal API key**:

- What it can do is what the key can do. For read-only use, create the key with only the **Read** permission: the two writes then fail on Linear's side and the reads keep working. Linear also has a read-only endpoint, `https://mcp.linear.app/mcp/readonly`; to use it, change `url` in the manifest and drop the two write tools.
- The key reaches only `mcp.linear.app`: the URL is fixed in the manifest, https only, redirects are refused, environment proxies are ignored, and it is never logged (every response is redacted with it).

## Install (owner, in the Mini's terminal)

1. In Linear's settings, under your account's security / API section, create a **personal API key**. Pick the permissions you want (Read only, or Read + Write for `save_issue` and `save_comment`) and the teams it may see.
2. `resonant secret set linear/token` and paste the key.
3. `resonant ext add repo:linear`. Listing the server's tools needs the key, so it first asks `Send linear/token to mcp.linear.app now?`; answer `y`. It then shows every tool with the pin of what the server reports (`sha256` of its name and the stripped input and output schemas, what the model sees), the server's unlisted tools as a note, and the escalations (`mcp server "linear" calls https://mcp.linear.app/mcp (http, network), sends secret token as its bearer token`, the two writes at L2). Type `linear` to install. The daemon restarts and texts you.
4. Try it: "what's on my Linear", "make a Linear ticket for the flaky login test" (asks first).

## When Linear changes a tool

Linear names its tools and changes their descriptions and schemas without notice. Each daemon start, and each `tools/list_changed` from the server, re-lists the tools: one whose pin changed is **disabled** (hidden from the model, refused on call) and you get one text, `linear: MCP tool <tool> changed since you approved it; disabled. resonant ext update linear to review`. `resonant ext update linear` shows the old and new pins; confirm to re-pin.

If `ext add` or `ext update` says `mcp server linear has no tool <name> (renamed? it has <names>)`, Linear renamed it (in 2026 `create_comment` became `save_comment`): change the name in `resonant.yaml`, commit, and run the command again. The install refuses rather than skipping the tool, because the manifest is your typed list of what each tool is. The names here were checked in October 2026 against public listings built from the live server (Glama's connector changelog, Speakeasy's catalog), not a live `tools/list` with a key; the live `tools/list` is the source of truth.

Linear's schemas are generated with zod and carry long ECMA-262 `pattern`s (for example `save_issue`'s `slaBreachesAt`, a 293-character ISO date-time regex). Resonant drops a pattern that is over 100 characters or that Python's `re` can't compile: the model doesn't see it and it isn't checked locally, and Linear still validates. Before LN-1 such a pattern refused the install with `its schema has a bad 'pattern' value`.

## Not here

No jobs, notifications or briefing summary (Linear's webhooks need an inbound URL, and the Mini takes none). No OAuth, no MCP resources or prompts.
