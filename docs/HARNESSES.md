# Connecting an agent harness

Carvera AI offers three interfaces. Use as many as your harness supports:

- **The skill** (`skill/carvera-ai/SKILL.md`): the agent's playbook. Harnesses that support Agent Skills
  load it on demand; others can read it as an instructions file.
- **The MCP server** (`mcp/carvera_mcp.py`, stdio): 24 tools for status, points, motion, probing, tool
  changes, vacuum and G-code. Needs `pip install -r requirements-mcp.txt`.
- **The HTTP API** (`http://127.0.0.1:17916`) and the `carvera-api` / `carvera-open` commands: for any
  agent that can run shell commands.

An agent can act as a **controller** (moves the machine when you ask) or as a **monitor** (reads
`machine_status`, `GET /api/job/<id>` and `GET /api/activity`, and reports). Several agents can watch
at once, because the server holds the machine's single connection and every action is logged with its
source.

In the examples, `$REPO` is the path to your `carvera-ai` clone. Only Claude Code has been tested
against the real machine; the others are compatible by protocol.

## Claude Code (Anthropic)

```sh
ln -s "$REPO/skill/carvera-ai" ~/.claude/skills/carvera-ai
mkdir -p ~/.local/bin && ln -s "$REPO/skill/carvera-ai/bin/"carvera-* ~/.local/bin/
claude mcp add carvera --scope user -- python3 "$REPO/mcp/carvera_mcp.py"
```

Use `/carvera-ai <task>`, or just ask about the Carvera. Symlinks (not copies) let `carvera-open` find the
repository. Otherwise set `CARVERA_CONTROL_HOME=$REPO`.

## Claude Desktop

Add the MCP server to `claude_desktop_config.json`:

```json
{ "mcpServers": { "carvera": { "command": "python3", "args": ["/path/to/carvera-ai/mcp/carvera_mcp.py"] } } }
```

Paste the contents of `SKILL.md` into a Project's instructions, or attach it to the conversation.

## OpenAI Codex CLI

```toml
# ~/.codex/config.toml
[mcp_servers.carvera]
command = "python3"
args = ["/path/to/carvera-ai/mcp/carvera_mcp.py"]
```

If your Codex version supports Agent Skills, link `skill/carvera-ai` into its skills folder
(`~/.codex/skills/carvera-ai`). Otherwise add a line to your `AGENTS.md`: "For the Carvera CNC, follow
/path/to/carvera-ai/skill/carvera-ai/SKILL.md". Put `carvera-open` and `carvera-api` on your `PATH`.

## Grok CLI (xAI)

Grok's CLI reads Claude-style skills from `~/.claude/skills`, so the Claude Code symlink above makes
`carvera-ai` available to it as well. Register the MCP server in Grok's MCP settings with the same
command (`python3 /path/to/carvera-ai/mcp/carvera_mcp.py`).

## Cursor

```sh
mkdir -p ~/.cursor/skills && ln -s "$REPO/skill/carvera-ai" ~/.cursor/skills/carvera-ai
```

```json
// ~/.cursor/mcp.json
{ "mcpServers": { "carvera": { "command": "python3", "args": ["/path/to/carvera-ai/mcp/carvera_mcp.py"] } } }
```

## Gemini CLI (Google)

```json
// ~/.gemini/settings.json
{ "mcpServers": { "carvera": { "command": "python3", "args": ["/path/to/carvera-ai/mcp/carvera_mcp.py"] } } }
```

Point your `GEMINI.md` at `skill/carvera-ai/SKILL.md`.

## VS Code agent mode, Windsurf, Cline, Zed, Continue and other MCP clients

Register a stdio MCP server named `carvera` with command `python3` and argument
`/path/to/carvera-ai/mcp/carvera_mcp.py`. Then give the agent `SKILL.md` as rules or instructions.

## Any shell-capable agent (no MCP)

```sh
export CARVERA_CONTROL_HOME=/path/to/carvera-ai
"$CARVERA_CONTROL_HOME/skill/carvera-ai/bin/carvera-open" --no-ui     # start, connect, print context
"$CARVERA_CONTROL_HOME/skill/carvera-ai/bin/carvera-api" GET /api/status
```

`carvera-open` and `carvera-api` are zsh scripts (macOS default; `apt install zsh` on Linux). The HTTP
API itself needs nothing but `curl`.

## Phone and tablet setup from any harness

The phone setup in `SKILL.md` section 9 needs only the HTTP API (`GET` / `POST /api/remote`), so any
harness that can run `carvera-api` or `curl` can walk you through it. Paste the prompt from
[PHONE.md](PHONE.md#set-it-up-with-your-agent-recommended); in a harness without slash commands, start
with "Read skill/carvera-ai/SKILL.md, then set up phone control for me".

## Environment variables

| Variable | Default | Used by |
|---|---|---|
| `CARVERA_PORT` | `17916` | server, MCP server, `carvera-open`, `carvera-api` |
| `CARVERA_CONTROL_HOME` | the repository the script lives in | `carvera-open` |
| `CARVERA_PYTHON` | `python3` | `carvera-open`, `Carvera Control.command` |
