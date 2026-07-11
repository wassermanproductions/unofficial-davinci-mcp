# Free-edition live bridge

Live tools in this server normally need **DaVinci Resolve Studio**, because
external scripting is a Studio feature. The **free edition** blocks outside
processes — but it still runs scripts *inside* the app, and a script launched
from Resolve's own **Scripts** menu is handed the same scripting object Studio
exposes.

This bridge uses that: you launch one small script from inside Resolve, and it
opens a private connection on your own machine that the MCP server talks to. From
then on, every live tool — build timelines, add markers, grades and LUTs, queue
renders — works on the free edition, exactly as it does on Studio.

## Setup

**1. Install the bridge script into Resolve's Scripts folder.**

```bash
python -m davinci_mcp.install_bridge
```

That copies `resolve_bridge.py` into your per-user Resolve scripts folder:

- **macOS:** `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/`
- **Linux:** `~/.local/share/DaVinciResolve/Fusion/Scripts/Utility/`

Prefer to do it by hand? Just copy `bridge/resolve_bridge.py` into the folder
above. Putting it under **Utility** makes it appear on every page.

**2. Start the bridge inside Resolve — once per session.**

Open DaVinci Resolve, then choose **Workspace ▸ Scripts ▸ resolve_bridge**.

You'll see a confirmation in the console (edition, version, and the local
address). Leave Resolve open and go back to your agent. Run `resolve_capabilities`
— the tier is now **live**, with the reason *"free edition via in-app bridge."*

Restarted Resolve? Just run the script again.

## What it does

The script starts a small web service that listens **only on your own computer**
(`127.0.0.1`) and runs a fixed, read-and-edit set of Resolve scripting calls on
your behalf. It writes a tiny discovery file so the MCP server can find it:

```
~/.config/unofficial-davinci-mcp/bridge.json
```

The file holds the local port and a one-time access token, and is created
readable only by you (permissions `0600`). It's removed when Resolve quits.

## Security

The bridge is built to be safe to leave running while you edit:

- **Local only.** It binds `127.0.0.1`, so nothing off your machine can reach it.
- **Token required.** Every request must carry a random token generated fresh
  each session and stored only in your private discovery file.
- **Fixed command set.** Only known scripting calls are allowed — the same ones
  the live tools already use (names beginning `Get`, `Set`, `Add`, `Append`,
  `Create`, `Import`, `Export`, `Render`, and so on). Anything else is refused,
  so the bridge can't be steered into arbitrary code.
- **You start it.** It only runs after you launch it from Resolve's menu, and it
  stops when you close Resolve.

## Troubleshooting

- **Capabilities still says interchange.** Make sure Resolve is open and you ran
  **Workspace ▸ Scripts ▸ resolve_bridge** this session. Re-run it after any
  Resolve restart.
- **The script isn't in the Scripts menu.** Confirm it's in the Utility folder
  above, then restart Resolve so it re-scans the folder.
- **Nothing prints when you run it.** Open **Workspace ▸ Console**, switch it to
  Py3, and run the script from there — the console shows any message.
- **Switched to Studio.** Nothing to change: the server prefers Studio's native
  scripting automatically and only falls back to the bridge when it needs to.

## Uninstall

Delete `resolve_bridge.py` from the Utility folder above. The discovery file is
cleaned up on quit; you can delete `~/.config/unofficial-davinci-mcp/bridge.json`
too if it's left behind.
