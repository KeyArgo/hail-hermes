# Hail Hermes — plugin-first design (agreed)

Owner's brief (2026-11-07): the **plugin is the product** — "the brains of the watch"; very
customizable; the watch is first-class and can change plugin settings itself; install must be
effortless; **any** model works (Hermes subscription, local LM Studio/Ollama/vLLM, free hosted)
and switching is trivial; real jobs are "will it rain?" and "continue the project I started days ago".

Sources: Claude Code (full design, `work/design-claude.md`), Codex (blocked by its own sandbox —
no design produced), spark-ab and spark-e (in flight). Decisions below are mine, grounded in the
real code and last night's reliability work.

## 1. The pane: five tabs, three depths

Each tab shows a **Glance** (≤3 controls), a **More** section (collapsed), and **Advanced**
(only with `ui_advanced`). Nobody sees a wall of options.

| Tab | Glance | More | Advanced |
|---|---|---|---|
| **Talk** | live watch mirror, hold-to-talk, interrupt | mode switch (Quick ask / Session), working count | — |
| **Brain** | **Who answers**: Quick model + Big-jobs model + [Test] · source status dots | **Add source** presets (LM Studio, Ollama, vLLM, OpenRouter free, Groq, Other) · hand-off switch · **home town for weather** | toolsets, timeouts, "find model servers on this computer" |
| **Character** | 8 skins, voice with ▶ preview | soul editor, face upload | raw voice mix, memory |
| **Sessions** | 3 pinned slots, recent sessions with [Pin] [Use on watch now] | new-session rule | source filter |
| **Watch** | "Let my watch connect", approval card, paired watch + battery | install wizard · **what the watch may change** | feature flags |

Header everywhere: `Nous · Quick ask · GLM-5.3 (sparks) · watch connected 82%` — each part links to its tab.

## 2. Watch: four jobs, no menus to hunt

| Job | Path |
|---|---|
| "Will it rain?" | Home screen → hold the mascot → ask. Weather is a **fast path** in the hub (3 s cap, Open-Meteo), answered as one sentence. |
| "Continue my project from Tuesday" | Tap the mode pill → **Recent** (title, project folder, age) → tap → the reply names the folder and what was happening. Voice search for long lists. |
| "Change how it behaves" | Gear → **Settings** → rows: Character · Voice · Model · Session · Speak replies. Each row is a picker showing the current value; the hub applies it live. |
| "Talk to it like Hermes" | Home → **Conversation** chip (hands-free back and forth). |

The watch may only change what the pane allows (`watch_can_change`): character, voice, model,
session, speak. Anything else returns `not_allowed` ("Change this on your computer").

**Models on the wrist are presets, not the whole catalogue** (owner, 2026-11-07): the pane picks
up to **five** models (`watch_model_presets`, as `source::model`), and the watch can only switch
between those five. They must be models **Hermes has already authorised** — an API key in `.env`,
an OAuth login in `auth.json`, or a local server that needs no key. The hub drops anything else
rather than offering a choice that would fail later.

## 3. Protocol additions (v2, additive, opt-in via `hello.settings = 1`)

- `get_settings` → `settings{rev, ...}` — the watch reads what it may change.
- `set_setting{key,value,req}` → `settings_changed{key,value,rev}` or `setting_error{code}`.
  Codes: `bad_setting`, `not_allowed`, `bad_value`, `model_unreachable`, `source_offline`,
  `bad_character`, `busy_turn`.
- `list_models` → `models[{source,id,label,tag,online,tools}]`, tag is `subscription|local|free`.
- `set_model{scope: quick|session, source, id}` — tested before saving; unreachable keeps the old one.
- `list_sessions{query,cursor}` → `sessions[{id,title,folder,age,source}]`; `recap{session}` (20 s cap).
- Watch-side timeouts: settings 6 s, models 6 s, set_model 10 s, sessions 12 s, recap 20 s.

## 4. Models: one list, every source

`models.py` owns sources and probing, with a 4 s total budget so one hung server cannot stall the picker:

- **Hermes providers** — `config.yaml` providers plus `provider_models_cache.json` (already implemented).
- **Local** — probe LM Studio (`:1234/v1/models`), Ollama (`:11434/api/tags`), vLLM (`:8000/v1/models`)
  when `autodetect_local` is on.
- **Free hosted** — OpenRouter free tier and Groq via keys in `secrets.json`.

Each entry is tagged `local` / `free` / `subscription`; offline sources are greyed with a reason
("LM Studio isn't running"). Switching applies on the **next** turn, never mid-turn, and the pane
must stop saying "restart required".

**Counterpoint from Codex (its sandbox blocked the repo, so this is scope advice only):** use one
generic OpenAI-compatible endpoint as the mechanism, and treat LM Studio / Ollama / vLLM / OpenRouter
as *presets that fill in a URL*, not as four separate integrations. That is less code, less risk, and
the same user experience. Adopted: presets are convenience; the engine underneath is one endpoint type.

## 5. Install with no terminal

Plugin: install it as a Hermes plugin, then **Settings → Plugins → Hail Hermes → on**, then Cmd-K →
Reload desktop plugins. Watch: **Watch tab → Install watch app** wizard — finds the watch over adb,
installs the APK, launches it, and shows the 4-digit code with an Approve button. No shell commands
in the happy path.

## 6. Build order (next 48 hours, one lane at a time)

| # | Work | Done means |
|---|---|---|
| 1 | `models.py`: sources, probe, cache, 4 s budget; `/models/all` route | pytest with fake LM Studio/Ollama/OpenRouter servers; one hung source cannot stall it |
| 2 | Watch settings: `get_settings` / `set_setting` / `settings_changed` plus `watch_can_change` | protocol tests including `not_allowed`; pane updates live |
| 3 | Hot-swap: `set_model`, applied after the current turn ends | switching mid-turn applies on the next turn; no "restart" prompt |
| 4 | Sessions: `list_sessions` with folder and age, plus `recap` | resuming an old session names its folder and the last thing that happened |
| 5 | Weather fast path and `home_place` | stubbed Open-Meteo test; 3 s cap; plain sentence on failure |
| 6 | Watch UI: Settings rows (Character/Voice/Model/Session/Speak), session picker with Recent | all four jobs run on the physical watch |
| 7 | Pane: **Brain** tab (Who answers, source status, Add source, home town) | e2e screenshots of ok / offline / needs-key / testing states |
| 8 | Install wizard and FirstRun card | installed on the Galaxy Watch from a reset state, no terminal |

**Cut:** encrypted LAN transport (say so in the README), Play Store listing, adding sources from the
watch, typing searches on the watch, per-watch settings, live token streaming, voice cloning, the phone
app, and the catalog PR / public repo until the owner decides.

## 7. Non-negotiables (from last night)

Never give up reconnecting (measured 4 s recovery) · one hub per state directory · a wedged
Speaking/Thinking state escapes itself · pairing refuses expired, replayed and disconnected approvals.
