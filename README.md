<div align="center">

# ⟡ morphyModules

### A small, sharp module store for [Shizu](https://github.com/ibeswipin/Shizu)

[![Shizu](https://img.shields.io/badge/for-Shizu-8B5CF6?style=flat-square)](https://github.com/ibeswipin/Shizu)
[![Modules](https://img.shields.io/badge/modules-6-0EA5E9?style=flat-square)](#modules)
[![Maintained by](https://img.shields.io/badge/maintained%20by-why--morphy-111827?style=flat-square)](https://github.com/why-morphy)

`AI tools` · `automation` · `utilities` · `media`

<sub>⟡ why-morphy — Shizu modules</sub>

</div>

`morphyModules` is a personal Shizu module store: practical tools I use, keep,
and improve. Add the store once, then install only the modules you want.

## Modules

| Module | Install name | What it does | Commands |
| :-- | :-- | :-- | :-- |
| 💱 **CurrencyTracker** | `currency` | Tracks exchange rates and alerts when they cross your target. | `.track` · `.untrack` · `.rates` · `.rate` |
| ✦ **AIManager** | `AIManager` | Owner-only manager for Codex CLI and Claude Code: installation, updates, browser sign-in, usage dashboard, and reset clocks. | `.ai` |
| ↗ **Forwarder** | `Forwarder` | Owner-only forwarding of new messages from one private chat to a channel. | `.forwarder` |
| 🖼️ **Upscayl** | `upscayl` | Upscales images with Upscayl / Real-ESRGAN models. | `.upscale` · `.upmodels` · `.upcancel` · `.upsetup` |
| ✨ **RandomReact** | `RandomReact` | Adds random reactions in selected groups. | `.react [chance]` |
| 💬 **RandomReply** | `RandomReply` | Randomly reacts to and replies in selected groups. | `.rrtoggle` |

> [!TIP]
> Open `.config <ModuleName>` in Shizu to see each module’s available settings.
> For example, AIManager lets you configure its menu lifetime and provider timeout.

## Install

### Add the whole store

1. Open `.config ShizuLoader` in Shizu.
2. Set **repo** to:

   ```text
   https://github.com/why-morphy/morphyModules/tree/main/modules
   ```

3. Browse and install modules:

   ```text
   .dlmod
   .dlmod AIManager
   ```

### Install one module directly

```text
.dlmod https://raw.githubusercontent.com/why-morphy/morphyModules/main/modules/RandomReact.py
```

Replace `RandomReact.py` with the file you want from [`modules/`](modules).

## A note on dependencies

Some modules need extra Python packages. Shizu’s BeSafe will show the
requirements declared in a module’s `# required:` header and ask for approval
before loading it. Only approve dependencies you trust.

## Add a module

The store index lives in [`modules/all.txt`](modules/all.txt). To add a module:

1. Create `modules/<name>.py`.
2. Add `<name>` on its own line in `modules/all.txt`.
3. Keep the name’s casing identical—the loader uses it as the install name.

## Credits

RandomReact is based on [sh1tn3t’s Sh1t-UB module](https://github.com/sh1tn3t/Sh1t-UB).
RandomReply originates from an archfemboy Hikka module. Their attribution and
license notices remain in the corresponding source files.

<div align="center">

<sub>Made for the Shizu ecosystem · maintained by <a href="https://github.com/why-morphy">why-morphy</a></sub>

</div>
