# pdf4me-mcp

The PDF4me MCP Server lets AI assistants use PDF4me's API through the Model Context Protocol (MCP) to handle different PDF and document tasks easily.

---

## 🔑 Get API Key

1. Sign up at [pdf4me.com](https://pdf4me.com)
2. Get your API key from the dashboard

---

## 📦 Install UV

You need [UV](https://docs.astral.sh/uv/) (a fast Python packaging tool) to run this MCP server.

**macOS / Linux**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (PowerShell)**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**Alternative methods**

```bash
# Homebrew (macOS)
brew install uv

# pipx
pipx install uv

# pip
pip install uv
```

For more options, see the [UV installation guide](https://docs.astral.sh/uv/getting-started/installation/).

---

## ⚙️ Setup in MCP Clients

### VS Code

Open `~/.config/Code/User/mcp.json` (macOS/Linux) or `%APPDATA%\Code\User\mcp.json` (Windows) and add:

```json
{
  "servers": {
    "pdf4me-mcp-std": {
      "type": "stdio",
      "command": "uvx",
      "args": ["pdf4me-mcp"],
      "env": {
        "API_KEY": "your-api-key-here"
      }
    }
  }
}
```

### Claude Desktop

Open the Claude Desktop config file:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "pdf4me-mcp": {
      "command": "uvx",
      "args": ["pdf4me-mcp"],
      "env": {
        "API_KEY": "your-api-key-here"
      }
    }
  }
}
```

### Cursor

Open Cursor settings → MCP, or edit `~/.cursor/mcp.json` (macOS/Linux) / `%USERPROFILE%\.cursor\mcp.json` (Windows):

```json
{
  "mcpServers": {
    "pdf4me-mcp": {
      "command": "uvx",
      "args": ["pdf4me-mcp"],
      "env": {
        "API_KEY": "your-api-key-here"
      }
    }
  }
}
```

### Windsurf

Edit `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "pdf4me-mcp": {
      "command": "uvx",
      "args": ["pdf4me-mcp"],
      "env": {
        "API_KEY": "your-api-key-here"
      }
    }
  }
}
```

### Continue (VS Code / JetBrains extension)

Add to your `~/.continue/config.json`:

```json
{
  "mcpServers": [
    {
      "name": "pdf4me-mcp",
      "command": "uvx",
      "args": ["pdf4me-mcp"],
      "env": {
        "API_KEY": "your-api-key-here"
      }
    }
  ]
}
```

---

## 🪟 Windows Note

On Windows, `uvx` may need to be called with its full path if it is not on your `PATH`. Replace `"command": "uvx"` with the full path, e.g.:

```json
"command": "C:\\Users\\<YourUser>\\.local\\bin\\uvx"
```

---

## 🛠️ Manual Run (without a client)

```bash
uvx pdf4me-mcp
```

Or, if installed locally:

```bash
API_KEY=your-api-key-here pdf4me-mcp
```
