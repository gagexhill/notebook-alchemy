# Installs Notebook Alchemy on Windows in one step:
#
#   irm https://raw.githubusercontent.com/gagexhill/notebook-alchemy/main/install/install.ps1 | iex
#
# It installs uv (Astral's Python tool installer) if it is missing, installs notebook-alchemy as a uv
# tool, installs the Chromium browser used for Google sign-in, puts a "Notebook Alchemy" shortcut on
# the desktop, and runs `notebook-alchemy test`. Running it again updates the install in place.
#
# It never asks for or stores a password, key or account, and writes only inside your user folders:
# uv's own folders, the Playwright browser folder, the desktop, the work folder named below and
# %LOCALAPPDATA%\notebook-alchemy (the shortcut's icon).
#
# Optional settings, read from the environment:
#   ALCHEMY_INSTALL_SOURCE        what `uv tool install` installs (default: the source below)
#   ALCHEMY_INSTALL_ICON_BASE     the folder URL the shortcut icon is downloaded from (default below)
#   ALCHEMY_INSTALL_WORK_DIR      the folder the shortcut opens in, where settings (.env) and
#                                 output are kept (default: "Notebook Alchemy" in your home folder)
#   ALCHEMY_INSTALL_SHORTCUT_DIR  where the shortcut goes (default: your desktop)
# uv's own variables (UV_TOOL_DIR, UV_TOOL_BIN_DIR, UV_INSTALL_DIR) and PLAYWRIGHT_BROWSERS_PATH are
# honoured as their tools document. UV_NO_MODIFY_PATH=1 also stops this installer changing your PATH.

# The source archive of the main branch on GitHub. An archive, not git+https, so git is not needed.
# Once the package is on PyPI (issue #33) this becomes 'notebook-alchemy'.
$NotebookAlchemySource = 'https://github.com/gagexhill/notebook-alchemy/archive/refs/heads/main.tar.gz'
# The generated icon package (scripts/brand_icons.py writes it from docs/brand/icon.svg).
$NotebookAlchemyIconBase = 'https://raw.githubusercontent.com/gagexhill/notebook-alchemy/main/docs/brand/icons'

function Install-NotebookAlchemy {
    # Everything runs inside this function so that `irm | iex` changes nothing in the caller's
    # session except defining this function and the source variable above.
    $ErrorActionPreference = 'Stop'
    # Exit codes of uv and Python are checked one by one below (PowerShell 7.4 could otherwise turn
    # them into errors with a less useful message).
    $PSNativeCommandUseErrorActionPreference = $false
    Set-StrictMode -Version 3.0

    $total = 5
    $source = $NotebookAlchemySource
    if ($env:ALCHEMY_INSTALL_SOURCE) { $source = $env:ALCHEMY_INSTALL_SOURCE }

    function Write-Step([int]$Number, [string]$Label) {
        Write-Host ''
        Write-Host "Step $Number of ${total}: $Label"
    }

    function Fail([string]$Message, [string]$Next) {
        # Caught at the bottom of the script, which prints it as "Error:" plus the next step.
        throw [System.Exception]::new("$Message`n$Next")
    }

    function Find-Uv {
        $found = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($found) { return $found.Source }
        # Where Astral's installer puts uv when the current window's PATH does not list it yet.
        $candidates = @()
        if ($env:UV_INSTALL_DIR) { $candidates += (Join-Path $env:UV_INSTALL_DIR 'uv.exe') }
        if ($env:XDG_BIN_HOME) { $candidates += (Join-Path $env:XDG_BIN_HOME 'uv.exe') }
        if ($env:XDG_DATA_HOME) { $candidates += (Join-Path $env:XDG_DATA_HOME '..\bin\uv.exe') }
        $candidates += (Join-Path $HOME '.local\bin\uv.exe')
        foreach ($candidate in $candidates) {
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
        return $null
    }

    function Invoke-Checked([string]$What, [string]$Next, [scriptblock]$Command) {
        & $Command
        if ($LASTEXITCODE -ne 0) { Fail "$What failed (exit code $LASTEXITCODE)." $Next }
    }

    function Write-InstallWarning([string]$Message) {
        [Console]::Error.WriteLine("Warning: $Message")
    }

    # Downloads the shortcut icon and returns its path, or $null with a warning. A missing icon
    # leaves the default one on the shortcut, so it never stops the install.
    function Get-ShortcutIcon {
        $base = $NotebookAlchemyIconBase
        if ($env:ALCHEMY_INSTALL_ICON_BASE) { $base = $env:ALCHEMY_INSTALL_ICON_BASE }
        $url = $base.TrimEnd('/') + '/notebook-alchemy.ico'
        $dir = Join-Path $env:LOCALAPPDATA 'notebook-alchemy'
        $dest = Join-Path $dir 'notebook-alchemy.ico'
        $download = "$dest.download"
        # Windows PowerShell's progress bar makes a download many times slower.
        $ProgressPreference = 'SilentlyContinue'
        try {
            $null = New-Item -ItemType Directory -Force -Path $dir
            Invoke-WebRequest -Uri $url -OutFile $download -UseBasicParsing
            # An .ico file starts with 00 00 01 00; anything else (an error page) is not used.
            $head = [System.IO.File]::ReadAllBytes($download) | Select-Object -First 4
            if (($head -join ',') -ne '0,0,1,0') { throw "[$url] is not an .ico file." }
            Move-Item -LiteralPath $download -Destination $dest -Force
            return $dest
        } catch {
            if (Test-Path -LiteralPath $download) { Remove-Item -LiteralPath $download }
            if (Test-Path -LiteralPath $dest -PathType Leaf) {
                Write-InstallWarning "Could not download the shortcut icon ($($_.Exception.Message)); using the one from the last install."
                return $dest
            }
            Write-InstallWarning "Could not download the shortcut icon ($($_.Exception.Message)); the shortcut uses the PowerShell icon. Run this installer again later to add it."
            return $null
        }
    }

    function Test-OnPath([string]$Dir) {
        $want = $Dir.TrimEnd('\')
        foreach ($entry in ($env:Path -split ';')) {
            if ($entry -and ($entry.TrimEnd('\') -ieq $want)) { return $true }
        }
        return $false
    }

    Write-Host 'Installing Notebook Alchemy.'

    # 1. uv
    Write-Step 1 'Finding uv, the Python tool installer'
    $uv = Find-Uv
    if ($uv) {
        Write-Host "Found uv at [$uv]."
    } else {
        Write-Host 'uv is not installed. Installing it with Astral''s installer from https://astral.sh/uv ...'
        # A separate PowerShell process, so that the installer cannot end this one.
        Invoke-Checked 'Installing uv' 'Check your internet connection, or install uv yourself (https://docs.astral.sh/uv/getting-started/installation/), then run this installer again.' {
            powershell.exe -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex'
        }
        $uv = Find-Uv
        if (-not $uv) {
            Fail 'uv was installed but cannot be found.' 'Open a new terminal and run this installer again.'
        }
        Write-Host "Installed uv at [$uv]."
    }

    # 2. notebook-alchemy
    Write-Step 2 'Installing notebook-alchemy'
    Write-Host "From [$source]. This can take a minute the first time."
    # --reinstall-package rebuilds from the source every time, so running this again picks up the
    # newest code even when the version number has not changed. --with-executables-from also puts
    # the `notebooklm` command (sign-in and uploads) on PATH; uv links only notebook-alchemy otherwise.
    # --python 3.12 is a tested version; uv downloads it if it is missing.
    Invoke-Checked 'Installing notebook-alchemy' 'Check your internet connection and run this installer again. The messages above say what uv could not do.' {
        & $uv tool install --upgrade --reinstall-package notebook-alchemy --with-executables-from notebooklm-py --python 3.12 $source
    }
    $toolDir = (& $uv tool dir).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $toolDir) { Fail 'Could not find uv''s tool folder.' 'Run `uv tool dir` to see why.' }
    $binDir = (& $uv tool dir --bin).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $binDir) { Fail 'Could not find uv''s command folder.' 'Run `uv tool dir --bin` to see why.' }
    $exe = Join-Path $binDir 'notebook-alchemy.exe'
    $python = Join-Path $toolDir 'notebook-alchemy\Scripts\python.exe'
    foreach ($path in @($exe, (Join-Path $binDir 'notebooklm.exe'), $python)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            Fail "uv reported success, but [$path] does not exist." 'Run `uv tool list` to see what is installed, then run this installer again.'
        }
    }
    if (Test-OnPath $binDir) {
        Write-Host "The notebook-alchemy command is in [$binDir], which is on your PATH."
    } elseif ($env:UV_NO_MODIFY_PATH) {
        # The shortcut and the steps below use the full path, so the install still works.
        Write-InstallWarning "UV_NO_MODIFY_PATH is set, so [$binDir] was not added to your PATH. Add it yourself to run notebook-alchemy from any terminal."
    } else {
        Write-Host "Adding [$binDir] to your PATH so that new terminals find notebook-alchemy."
        & $uv tool update-shell
        if ($LASTEXITCODE -ne 0) {
            Write-InstallWarning "Could not add [$binDir] to your PATH (exit code $LASTEXITCODE). The shortcut still works; add the folder yourself to run notebook-alchemy from any terminal."
        }
    }
    if (-not (Test-OnPath $binDir)) { $env:Path = "$binDir;$env:Path" }

    # 3. Chromium for sign-in
    Write-Step 3 'Installing the browser used for Google sign-in (Chromium, about 150 MB the first time)'
    Invoke-Checked 'Installing Chromium' 'Check your internet connection and run this installer again. You can also sign in later with `notebook-alchemy login --browser-cookies chrome` instead.' {
        & $python -m playwright install chromium
    }

    # 4. Shortcut
    Write-Step 4 'Creating the desktop shortcut'
    $workDir = Join-Path $HOME 'Notebook Alchemy'
    if ($env:ALCHEMY_INSTALL_WORK_DIR) { $workDir = $env:ALCHEMY_INSTALL_WORK_DIR }
    $shortcutDir = [Environment]::GetFolderPath('Desktop')
    if ($env:ALCHEMY_INSTALL_SHORTCUT_DIR) { $shortcutDir = $env:ALCHEMY_INSTALL_SHORTCUT_DIR }
    if (-not $shortcutDir) { Fail 'Could not find your desktop folder.' 'Set ALCHEMY_INSTALL_SHORTCUT_DIR to the folder for the shortcut and run this installer again.' }
    $null = New-Item -ItemType Directory -Force -Path $workDir
    # An absolute path, so the shortcut and the self-test use the same folder.
    $workDir = (Resolve-Path -LiteralPath $workDir).ProviderPath
    $shortcutPath = Join-Path $shortcutDir 'Notebook Alchemy.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($shortcutPath)
    $link.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    # -NoExit keeps the window open after the command ends, so its output can be read and more
    # commands typed. The path is single-quoted for PowerShell, with every kind of single quote
    # (PowerShell also treats curly ones as quotes) doubled.
    $quotedExe = "'" + [System.Management.Automation.Language.CodeGeneration]::EscapeSingleQuotedStringContent($exe) + "'"
    $link.Arguments = "-NoExit -NoLogo -NoProfile -Command `"& $quotedExe`""
    $link.WorkingDirectory = $workDir
    $link.Description = 'Turn a YouTube channel into NotebookLM sources'
    $icon = Get-ShortcutIcon
    if ($icon) { $link.IconLocation = "$icon,0" }
    $link.Save()
    if (-not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) {
        Fail "The shortcut [$shortcutPath] was not created." 'Check that you can write to that folder, then run this installer again.'
    }
    Write-Host "Created [$shortcutPath]. It opens in [$workDir], where your settings and output are kept."

    # 5. Self-test
    Write-Step 5 'Checking the install'
    Push-Location -LiteralPath $workDir
    try {
        Invoke-Checked 'The self-test' 'Read the failed checks above, then report them at https://github.com/gagexhill/notebook-alchemy/issues.' {
            & $exe test
        }
    } finally {
        Pop-Location
    }

    Write-Host ''
    Write-Host 'Notebook Alchemy is installed.'
    Write-Host 'To start, open [Notebook Alchemy] on your desktop, or open a new terminal and run:'
    Write-Host '  $ notebook-alchemy'
}

try {
    Install-NotebookAlchemy
} catch {
    $lines = $_.Exception.Message -split "`n", 2
    [Console]::Error.WriteLine("Error: $($lines[0])")
    if ($lines.Count -gt 1 -and $lines[1]) {
        [Console]::Error.WriteLine($lines[1])
    } else {
        # An error that did not come from Fail, for example a folder that cannot be written.
        [Console]::Error.WriteLine('Run this installer again. If it fails the same way, report the message above at https://github.com/gagexhill/notebook-alchemy/issues.')
    }
    # Run as a file, report failure in the exit code. Run through `irm | iex`, leave the window open
    # and set LASTEXITCODE for anything that checks it afterwards.
    if ($PSCommandPath) { exit 1 }
    $global:LASTEXITCODE = 1
}
