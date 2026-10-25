# Skills: MCP in both directions

[MCP](https://modelcontextprotocol.io) (Model Context Protocol) is the open standard for giving an AI tools and data. This project uses it in
two directions.

## 1. The assistant uses MCP servers as skills (the dashboard's **Skills** page)

Connect any MCP server that speaks Streamable HTTP (a notes service, a calendar, a search service, your own): give it a name, its address
and, if it needs one, a token. The server's tools are listed and the assistant can use them in a chat, the same way it uses its own
look-ups ("find my note about vitamins", "what is on my calendar tomorrow").

| Tool | What happens |
|---|---|
| The server marks it **read-only** (`readOnlyHint`) | runs at once |
| You switched on **Allow without asking** for it on the Skills page | runs at once |
| Anything else (it could change something) | the assistant shows a **confirmation card** in Telegram (the same card as for changes to your ledger); it runs only after you tap confirm, once |
| You switched it **Off** | never runs and is hidden from the assistant |

Safety rules that are always on:

- What a skill returns is **untrusted data, never instructions**; it is cut to 3,000 characters.
- Tokens are stored **encrypted** (`SECRETS_KEY`) and never shown again. A server that cannot be reached is not kept.
- At most `MCP_CALLS_PER_DAY` (30) skill calls a day; each call has a 40 second limit.
- Addresses in the link-local range (cloud metadata) are refused. Only Streamable HTTP servers: nothing is started on your server.
- The assistant sends the server what it needs to use the tool, and only the servers you connected can be called. Connect servers you
  trust.

## 2. Your own data as an MCP server (off by default)

Set `MCP_ENABLED=true` in `.env` and `./deploy.sh up`. Any MCP client (Claude Desktop, Claude Code, an IDE, your own agent) can then
connect to `http://YOUR-SERVER:8000/mcp` with your dashboard key (`X-API-Key: ...` or `Authorization: Bearer ...`) and read: who you are
(`about_me`), your diary (`diary`), what you used and watched (`activity`), your patterns (`profile`) and a search of what the assistant
remembers (`search_memory`). Every tool is read-only. `MCP_EXPOSE_FINANCE=true` also offers the money tools (spending, transactions, budgets,
balances); leave it off unless you want that.

What a connected client reads is sent to that client's model provider like a chat message, and anyone with your key can read it all: keep the
server on your home network or a VPN, and connect only clients you trust. Private journal entries are included only if you shared them.
