# morphyModules

Modules for the [Shizu](https://github.com/ibeswipin/Shizu) Telegram userbot.

| Module | File | What it does |
| --- | --- | --- |
| CurrencyTracker | `currency.py` | Tracks exchange rates and alerts when they rise or fall past a threshold. Commands: `.track`, `.untrack`, `.rates`, `.rate` |
| Upscayl | `upscayl.py` | AI image upscaling with Upscayl (upscayl-bin + Real-ESRGAN models). Commands: `.upscale`, `.upmodels`, `.upcancel`, `.upsetup` |

## Install

Send the raw file link to Shizu:

```
.dlmod <raw link to the module file>
```

Shizu's BeSafe shows the module's dependencies (the `# required:` header) for approval before it loads.
