# morphyModules

Modules for the [Shizu](https://github.com/ibeswipin/Shizu) Telegram userbot. The `modules/` folder is a Shizu module store: `all.txt` lists the module names and each one lives in `<name>.py` next to it.

| Module | Install name | What it does |
| --- | --- | --- |
| CurrencyTracker | `currency` | Tracks exchange rates and alerts when they rise or fall past a threshold. Commands: `.track`, `.untrack`, `.rates`, `.rate` |
| Upscayl | `upscayl` | AI image upscaling with Upscayl (upscayl-bin + Real-ESRGAN models). Commands: `.upscale`, `.upmodels`, `.upcancel`, `.upsetup` |
| RandomReact | `RandomReact` | Puts random reactions on messages in chosen groups (port of sh1tn3t's Sh1t-UB module). Command: `.react [chance]` |
| RandomReply | `RandomReply` | Randomly reacts to and replies to messages in chosen groups (port of archfemboy's Hikka module). Command: `.rrtoggle` |

## Install

Install a single module by its raw link:

```
.dlmod https://raw.githubusercontent.com/why-morphy/morphyModules/main/modules/RandomReact.py
```

Or use the whole store: open `.config ShizuLoader` and set `repo` to `https://github.com/why-morphy/morphyModules/tree/main/modules` (this replaces the default ShizuMods store). Then:

```
.dlmod                 lists the modules
.dlmod RandomReact     installs one by its install name
```

Shizu's BeSafe shows the module's dependencies (the `# required:` header) for approval before it loads.

## Adding a module

Put `<name>.py` in `modules/` and add `<name>` on its own line to `modules/all.txt`. Names are case-sensitive.
