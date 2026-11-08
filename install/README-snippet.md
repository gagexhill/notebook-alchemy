### Install

One command installs everything: [uv](https://docs.astral.sh/uv/) (if it is missing), Notebook Alchemy, and the Chromium browser it uses for Google sign-in. It then adds a **Notebook Alchemy** shortcut and runs the self-test. It asks for no password or key, and writes only inside your home folder.

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/gagexhill/notebook-alchemy/main/install/install.ps1 | iex
```

macOS and Linux:

```bash
curl -LsSf https://raw.githubusercontent.com/gagexhill/notebook-alchemy/main/install/install.sh | sh
```

The shortcut is on the desktop on Windows and macOS, and in the applications menu (and on the desktop, if there is one) on Linux. It opens a terminal in the `Notebook Alchemy` folder in your home folder, where your settings and output are kept, and starts `notebook-alchemy`, which walks you through signing in to NotebookLM, an optional free Gemini key and your first channel. Run the same command again to update to the newest version. Run it as your own user, not with `sudo`. To read a script before running it, open [`install/install.ps1`](install/install.ps1) or [`install/install.sh`](install/install.sh).

To remove it, delete the shortcut and the folder that holds its icon (`%LOCALAPPDATA%\notebook-alchemy` on Windows, `~/Library/Application Support/notebook-alchemy` on macOS, `~/.local/share/notebook-alchemy` on Linux; on Linux also delete `~/.local/share/applications/notebook-alchemy.desktop`), then run:

```bash
uv tool uninstall notebook-alchemy
```

The `Notebook Alchemy` folder keeps your settings and output until you delete it. Chromium stays in Playwright's `ms-playwright` folder (in your local app data on Windows, `~/Library/Caches` on macOS, `~/.cache` on Linux), where other tools may share it.
