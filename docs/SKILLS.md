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

### Servers that ask for a login (OAuth), for example a nutrition tracker

Some servers (the free open-source nutrition tracker at `https://nutrition-mcp.com/mcp`, for one) tie your data to an account. On the Skills page use
**Connect with login**: the page shows the server's login page; sign in there; the browser then ends on an error page whose address starts with
`http://localhost:8787/callback?code=...` (nothing listens there on purpose); copy that whole address and paste it back. The tokens are kept encrypted
and refreshed by themselves; if the login ever expires for good, the skill says so and you sign in again. The **Example: nutrition tracker** button fills
in the address, and its Specialist box can be filled with a ready-made coach role.

### Specialists: one conversation, the right agent

The main assistant (the master) knows every skill. For a domain that deserves its own manners, open a server's **Specialist** box and give it a **role**
(how the assistant behaves while it is in charge) and **topic words** (for example makan, kalori, air putih). When a message contains a topic word, the
assistant switches to that role and sees only that server's tools; a follow-up without a keyword ("tambah nasi putih") stays with it for 20 minutes.
Everything else goes to the master with all the skills.

Photos and files: send the bot a photo or a file and it answers like to a message. A receipt goes to the ledger as before. Anything else is read by the
vision model (the same one that reads receipts): what it is (a meal, a drink, a packaged product, a document, a screenshot, a person, a place, an
object), what it shows, the text printed on it (names, dates such as an expiry date), a barcode if it can be read, and for food the dishes with rough
portions. The assistant takes that as your message: food and products go to the nutrition tools when a nutrition server is connected (estimates, logged
after your tap), a document is summarised, a screenshot explained. It never says who a person is. What you sent is remembered for six hours, so "look at
the photo", "this" or a reply to the photo work, and the assistant can look at the picture again with a specific question ("what is the expiry date?").
Files: text, CSV, JSON and PDFs with a text layer are read and handed to the assistant; a PDF that is a bank statement is reconciled against the ledger as
before (and so is a PDF that cannot be read, for example a scan). An MCP tool itself takes text, numbers or links, not a photo.

### Routines: schedules and automations (the Routines page, or ask in chat)

A routine runs by itself at a time of day (on chosen days) or every N minutes (15 or more, between 08:00 and 21:30):

| Kind | What it does |
|---|---|
| **Remind** | a reminder in your own style ("log lunch") |
| **Task** | the assistant is given the task and does it like in a chat, with its look-ups and skills; anything it wants to change still needs your tap |
| **Skill call** | one tool of a connected server with fixed arguments, for example a smart-home action. You approved it when you created the routine, so it runs without asking, but only while the tool is still allowed on the Skills page |

Say "remind me to log lunch at 12:30 every day" in Telegram and the assistant shows a card; confirm it and the routine exists. Each run happens once, at
most 60 runs a day in all, and every failure is reported to you. Smart-home devices work the same way as any other skill: connect their MCP server
(for example a Home Assistant MCP server) on the Skills page, then add a Skill call routine.

## 2. Your own data as an MCP server (off by default)

Set `MCP_ENABLED=true` in `.env` and `./deploy.sh up`. Any MCP client (Claude Desktop, Claude Code, an IDE, your own agent) can then
connect to `http://YOUR-SERVER:8000/mcp` with your dashboard key (`X-API-Key: ...` or `Authorization: Bearer ...`) and read: who you are
(`about_me`), your diary (`diary`), what you used and watched (`activity`), your patterns (`profile`) and a search of what the assistant
remembers (`search_memory`). Every tool is read-only. `MCP_EXPOSE_FINANCE=true` also offers the money tools (spending, transactions, budgets,
balances); leave it off unless you want that.

What a connected client reads is sent to that client's model provider like a chat message, and anyone with your key can read it all: keep the
server on your home network or a VPN, and connect only clients you trust. Private journal entries are included only if you shared them.
