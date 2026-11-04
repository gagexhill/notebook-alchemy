# Google AI Studio blueprint

Reproduce what `notebook-alchemy run` sends to Gemini, so you can test a synthesis by hand in
[Google AI Studio](https://aistudio.google.com/) before spending API calls on a whole channel.

The settings and the system instruction below are mirrored in `ai_studio_agent_config.json`, and a test
(`tests/test_prompts.py`) fails if either drifts from `src/notebook_alchemy/synthesizer.py` and
`config.py`. The code is the source of truth.

## Settings

| Setting | Value | Why |
| :-- | :-- | :-- |
| Model | `gemini-2.5-flash-lite` (the tool's default, `ALCHEMY_GEMINI_MODEL`) | Not scheduled for shutdown; Google's pricing page lists it at the lowest Flash rate. `gemini-2.5-flash` shuts down on 2026-12-01 |
| Thinking budget | 0 | Thinking tokens bill at the output rate and synthesis from a transcript does not need them |
| Temperature | 0.2 | Keeps the output close to the transcript |
| Max output tokens | 32768 | A compendium covers up to 50 videos |
| Grounding and code execution | Off | The transcripts are the only source |

Check current rates on the [pricing page](https://ai.google.dev/gemini-api/docs/pricing) before estimating
a large run. `notebook-alchemy dry-run <url>` does the arithmetic from video lengths.

## System instruction

```text
You are a Principal Knowledge Architect. Synthesize raw video transcripts into a dense, authoritative domain compendium for Google NotebookLM. No filler, no preamble, no sponsor reads. Attribute every claim to its source video by number, like [3].
```

## User prompt shape

The tool sends one user message per compendium volume: the instructions, then each video as
`### [n] Title` followed by its transcript. Paste a few transcripts in that shape to try it.

```text
Synthesize the N numbered videos below, from '<source name>', into one Markdown compendium for the
domain '<domain>' (volume 1).

Rules:
1. Do not write YAML frontmatter, a title line, or a source table; those are added for you.
2. Use these sections: ## Executive Summary, ## Key Concepts and Mental Models,
   ## Procedures and Frameworks, ## Common Pitfalls.
3. Extract concrete steps, frameworks and examples the speakers actually give.
   Cite the source video number after each claim, like [3].
4. Remove filler, sponsor reads and intros. Do not invent facts that are not in a transcript.

Sources:

### [1] First video title
<transcript text>

### [2] Second video title
<transcript text>
```

The tool adds the frontmatter, the title line and a deterministic `## Sources` table itself, so every video
is listed even if the model's answer is cut short.
