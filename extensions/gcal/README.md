# gcal extension

Google Calendar for Resonant, through **Google's own remote MCP server** for Calendar (`https://calendarmcp.googleapis.com/mcp/v1`, streamable HTTP, [Google's docs](https://developers.google.com/workspace/calendar/api/guides/configure-mcp-server)). Both brains reach it: the local model and the Claude brain see these tools like any other Resonant tool, and every call goes through Resonant's gate, approvals, taint rules, audit log and kill switch. The claude.ai Calendar connector isn't used: it doesn't load under `claude --restricted`.

There is almost no code here. The manifest lists the server tools Resonant may use, each with its effect, level and a description we wrote, and `cli.py` holds one local tool (`draft_event`). See [`docs/extensions.md`](../../docs/extensions.md) §4, MCP tools, for what that guarantees.

| Tool | Level | What it does |
|---|---|---|
| `list_calendars` | L0 | The calendars you can see, with their ids. |
| `list_events` | L0 | Events in a time range. "What's on my calendar tomorrow" lands here. |
| `get_event` | L0 | One event, with attendees and details. |
| `search_events` | L0 | Events matching some text (a title, a person, a place). |
| `suggest_time` | L0 | Free/busy: open slots for you and others in a range. Google's server has no separate free/busy tool; this one reads free/busy and writes nothing. |
| `draft_event` | L0 | Local only: checks a proposed event (times, attendee emails) and says what `create_event` would do and who would get an invite. It never contacts Google and gets no secrets. |
| `create_event` | L2, commit, never `yes always` | Add an event. Asks a plain yes first. |
| `update_event` | L2, commit, never `yes always` | Change an event. Asks first. |
| `delete_event` | L2, commit, never `yes always` | Delete an event. Asks first. |
| `respond_to_event` | L2, commit, never `yes always` | Accept, decline or tentatively accept an invite. Asks first. |

Why these levels:

- **Reads are L0.** They change nothing. Every result counts as outside content, since an invite's title or notes can come from anyone (`brain.taints`: extension and MCP results always taint). So after any calendar read in a turn, a change asks the owner, and the prompt offers no `yes always`.
- **Drafting is L0.** `suggest_time` and `draft_event` write nothing anywhere. L3 means "act, then report", which fits a reversible write; there is no write here to report.
- **Writes are L2 with `commit: true` and `standing: false`.** The model's choice always asks a plain yes. A slash command you type yourself (`/gcal create_event …`) runs, because typing it is your consent. `commit` marks the four writes as irreversible to the outside world: each can email people (invites, updates, cancellations, an RSVP to the organizer), and an email can't be unsent.
- **Writes never take `yes always` (`standing: false`).** Each of the four can email other people, so every model-chosen calendar change asks, every time. Their prompt doesn't offer `yes always`, and an `allow` rule for one of them (`/rules allow running gcal.create_event`) never makes it run unasked; asking in chat to stop asking ("don't ask about calendar", `allow_tools`) is refused for them too. The level is still L2, a plain `yes`, not L1's `yes <code>`: by convention the code is for destructive actions, and a level belongs to the whole tool, so L1 would put a code on every personal event too.

The server offers tools this manifest doesn't list (Google can add more). Resonant never registers or calls a tool that isn't listed. `ext add` shows the unlisted ones as a note, and nothing is ever mapped to L0 automatically.

## Auth

Google's Calendar MCP server takes OAuth 2.0 access tokens from **your own Google Cloud OAuth client**. Nothing is hosted for you. An access token lasts about an hour, so the manifest uses `auth: oauth` (built for this extension, CAL-1). Three Keychain secrets are involved, `gcal/client_id`, `gcal/client_secret` and `gcal/refresh_token`:

- The daemon reads them from the Keychain when a session starts. It posts them only to `https://oauth2.googleapis.com/token` (fixed in the manifest; https only, redirects refused, environment proxies ignored) and gets back an access token.
- The access token lives only in the daemon's memory. It is sent only to `calendarmcp.googleapis.com` as `Authorization: Bearer`, refreshed two minutes before it expires (and once after a `401`), and redacted from everything the server sends.
- Nothing is written to disk. No process gets any of them: the MCP server is remote, and `draft_event` declares no secrets. The model never sees any of them. If the token endpoint refuses, only the RFC 6749 error code is shown (for example `invalid_grant`), never Google's own text.
- What Resonant can do is what the scopes you grant allow (below). With the read-only scopes, the four writes fail on Google's side and nothing changes.

## Install (owner, on the Mini)

You need a Google account and about 15 minutes. All browser steps are on Google's own pages. Never paste a secret into a chat, including with Resonant.

1. **The Developer Preview.** Google lists Calendar's MCP server as a Developer Preview. Its docs require membership in the [Google Workspace Developer Preview Program](https://developers.google.com/workspace/preview). Join it with the Google account you'll use, if you aren't already a member.
2. **A Google Cloud project.** In the [Cloud console](https://console.cloud.google.com/projectcreate), create a project, for example `resonant-calendar`.
3. **Enable two APIs** in that project: the Google Calendar API and the Google Calendar MCP API. Use the console's API Library, or run:
   ```sh
   gcloud services enable calendar-json.googleapis.com --project=PROJECT_ID
   gcloud services enable calendarmcp.googleapis.com --project=PROJECT_ID
   ```
   If the second one fails, the account isn't in the preview yet (step 1).
4. **The consent screen** ([Google Auth Platform](https://console.cloud.google.com/auth/overview)):
   - **Branding:** app name `Calendar MCP Server` (Google's suggestion), and your email as the support and developer contact.
   - **Audience:** `Internal` if your account is in a Workspace organization, otherwise `External`. For External, add yourself under **Test users**, then click **Publish app** (status *In production*). An app left in *Testing* issues refresh tokens that expire after 7 days. Google doesn't need to verify an app that only you use. Its consent page warns that the app is unverified: choose *Advanced → Go to Calendar MCP Server*.
   - **Data access:** add the scopes. Least privilege is **read-only**. Pick one set:
     - Read-only (lists, search, free time, drafts; the four writes fail on Google's side):
       `https://www.googleapis.com/auth/calendar.calendarlist.readonly`
       `https://www.googleapis.com/auth/calendar.events.readonly`
       `https://www.googleapis.com/auth/calendar.events.freebusy`
     - Read and write (adds create, update, delete and respond, each still asking you first):
       `https://www.googleapis.com/auth/calendar.calendarlist.readonly`
       `https://www.googleapis.com/auth/calendar.events`

     You never need the full `https://www.googleapis.com/auth/calendar` scope.
5. **The OAuth client** (Google Auth Platform → **Clients** → **Create client**):
   - Application type **Web application**, name `resonant`.
   - Tick **Use this client for an AI-powered agent** (Google's MCP servers reject tokens from clients without it; it can't be added to an existing client later).
   - Authorized redirect URI: `https://developers.google.com/oauthplayground` (step 6 uses it). Nothing else.
   - Create it. Keep the **Client ID** and **Client secret** on screen for steps 6 and 7.
6. **Get a refresh token** with Google's [OAuth 2.0 Playground](https://developers.google.com/oauthplayground):
   - Click the gear icon (top right) and tick **Use your own OAuth credentials**. Paste the client ID and secret. Leave **Access type: Offline** and **Force prompt: Consent Screen**: they make Google issue a refresh token.
   - In **Step 1**, type the scopes from step 4 into *Input your own scopes*, separated by spaces, then **Authorize APIs**. Sign in and allow.
   - In **Step 2**, click **Exchange authorization code for tokens** and copy the **Refresh token** (it starts with `1//`).
   - Close the tab. The token stays in your browser and the Keychain only.
7. **Store the three secrets**, in the Mini's terminal (each prompts; paste the value; nothing is echoed):
   ```sh
   resonant secret set gcal/client_id
   resonant secret set gcal/client_secret
   resonant secret set gcal/refresh_token
   resonant secret list gcal          # all three present
   ```
8. **Install:** `resonant ext add repo:gcal`.
   - Listing Google's tools needs an access token, so it asks first: `MCP server calendar at https://oauth2.googleapis.com/token needs gcal/refresh_token to list its tools. Send gcal/refresh_token to oauth2.googleapis.com now?`. Answer `y`: that sends the client ID and client secret to the same Google token endpoint, and nowhere else.
   - The review then shows every tool with its pin, the unlisted tools as a note, and the escalations: `mcp server "calendar" calls "https://calendarmcp.googleapis.com/mcp/v1" (http, network), sends secrets client_id, client_secret, refresh_token to "https://oauth2.googleapis.com/token" (oauth) and the access token it gets as its bearer token`, and the four writes at L2.
   - Type `gcal` to install. The daemon restarts and texts you.
9. **Try it** by text:
   - "what's on my calendar tomorrow": you get `looking at your calendar` (or another short ack), then Claude's answer from `list_events`.
   - "when am I free thursday afternoon" goes to `suggest_time`.
   - "put lunch with Sam on my calendar friday at noon" asks `… Reply yes / no`. `yes` creates it, once. With the read-only scopes, Google refuses the write and you're told it wasn't done.

## How asks are routed

Calendar questions are triaged to Claude (kind `calendar`). While this extension is loaded and the asker may use it, a calendar ask goes to Claude as an ordinary ask with Resonant's tools: for the owner that's a turn of the Claude brain session, which calls `list_events`, `create_event` and the rest over Resonant's MCP bridge. Without the extension (or with `brain.ask_claude.calendar_extension: null`), calendar asks take the old connector path, which stays refused under `--restricted`. The local model can also call these tools directly. Both brains go through the same gate.

## When it breaks

- **`the OAuth token endpoint refused the refresh token (invalid_grant)`**: the refresh token was revoked or expired. The causes are an app still in *Testing* (7 days), a password change, revoking access in your Google account, or more than 100 refresh tokens for one client. Redo step 6, then `resonant secret set gcal/refresh_token`. The next call reads the new one; no `ext update` is needed.
- **`invalid_client`**: the client ID or secret is wrong, or the client was deleted. Redo step 5 and re-set `gcal/client_id` and `gcal/client_secret`.
- **`the server refused the token (http 403)`** or a write refused: a scope is missing (writes need `calendar.events`), an API isn't enabled (step 3), the account isn't in the preview (step 1), or the client lacks the AI-agent setting (step 5: make a new client).
- **A tool changed.** Google can change a tool's schema. Resonant then disables that tool and texts `gcal: MCP tool <tool> changed since you approved it; disabled. resonant ext update gcal to review`. If `ext add` or `ext update` says `mcp server calendar has no tool <name>`, Google renamed it: fix the name in `resonant.yaml`, commit, and run the command again. The names here were checked against Google's MCP reference in October 2026. The live `tools/list` is the source of truth.

**Version.** There is no package to pin. The server is Google's hosted `/mcp/v1` endpoint, and what you approve is pinned per tool at install (`sha256` over each tool's name and stripped schemas). Any change disables that tool until you review it.

## Why not an open-source server

Every well-known open-source Google Calendar MCP server is a Node or Python package run over stdio. Resonant refuses those commands (`npx`, `node`, `uvx`: they run code the pin doesn't cover; [`docs/extensions.md`](../../docs/extensions.md) §4). They also read the OAuth client from a file and write their own token file under your home folder, outside the Keychain. Google's hosted server avoids all of that: nothing of it runs on the Mini, and the tokens stay in the Keychain and the daemon's memory.

## Not here

No email: Gmail stays off. No jobs, no notifications and no briefing summary (Calendar push notifications need an inbound URL, and the Mini takes none). No MCP resources or prompts.
