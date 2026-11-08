# Google AI Studio blueprint

Reproduce the request `notebook-alchemy run` sends to Gemini for each video, so you can try the
labelling by hand in [Google AI Studio](https://aistudio.google.com/) before spending API calls on a
whole channel.

The settings and the system instruction below are mirrored in `ai_studio_agent_config.json`, and a test
(`tests/test_prompts.py`) fails if either drifts from `src/notebook_alchemy/knowledge.py` and `config.py`.
The code is the source of truth.

## What the request does

Gemini does not write the books. It reads one video's transcript, already cleaned and split into numbered
chunks on your machine, and returns labels only: which runs of chunks to keep or drop, a short headline
for each kept run, how long the information stays true (its kind), and an optional editor's note. The
books are built from the speaker's own words with those labels, which is why a run costs a few percent
of the transcript in output instead of rewriting it. For captions without punctuation the answer also
carries the kept words with punctuation added; a local check rejects it if any word changed.

## Settings

| Setting | Value | Why |
| :-- | :-- | :-- |
| Model | `gemini-3.8-flash` (the tool's default, `ALCHEMY_GEMINI_MODEL`) | Labels are cached per model, so the tool never mixes in its fallback model for this step |
| Output | JSON (`responseMimeType: application/json`) | The answer is parsed and validated, never shown as prose |
| Thinking level | Low | Deciding what to keep needs little reasoning, and thinking tokens bill at the output rate |
| Max output tokens | 16384 | Labels are short; punctuated windows are cut to about 5,000 words to fit |
| Grounding and code execution | Off | The transcript is the only source |

Check current rates on the [pricing page](https://ai.google.dev/gemini-api/docs/pricing) before a large
run; `notebook-alchemy dry-run <url>` estimates the cost from what is already read and from video lengths.

## System instruction

```text
You label transcripts for a study library. You never rewrite or summarise the speaker's words; you only decide which numbered chunks are worth keeping, group them into points, and describe each point. Answer with JSON only.
```

## User prompt shape

One message per video (or per window of a very long one). Paste a real transcript's chunks in place of
the placeholders to try it.

```text
Video: <video title>
Published: <published date>
Today: <today>

Keep (in the speaker's words):
- step-by-step procedures and how-tos;
- specific facts: names of programs, agencies, systems, forms and codes; requirements, eligibility and
  thresholds; dollar amounts, deadlines and dates; websites and tools named;
- definitions and explanations of concepts, and the reasoning behind advice;
- frameworks, checklists and rules of thumb;
- examples, case stories and their outcomes, with numbers;
- mistakes, warnings and how to fix them;
- answers to viewer questions.
Drop:
- greetings, intros, outros and "in this video I'll..." previews;
- like, subscribe and bell requests; sponsor reads; promotion of the creator's own courses or coaching
  (unless the offer itself is the subject);
- filler, repetition of a point already made, small talk, jokes and off-topic tangents;
- pointers to links that carry no content.
When unsure whether something is useful, KEEP it.

kind, for each kept point:
- "lasting": a principle or explanation that stays true;
- "rule": a procedure or requirement tied to a website, regulation, program or system as it was then;
- "news": an event or announcement (a shutdown, an executive order, a policy just changed);
- "figure": prices, grant amounts, deadlines, rates or other numbers that change.

Group chunks 1 to 2 into consecutive segments that cover every chunk exactly once, in order. A kept segment is one coherent point (usually 1 to 8 chunks). Use the speaker's language for headlines and notes.
Return only JSON:
{"segments": [{"from": <first chunk>, "to": <last chunk>, "keep": true|false, "headline": "<at most 14 words, kept segments only>", "kind": "lasting|rule|news|figure", "note": "<optional, at most 25 words: context the passage needs to stand alone>"}]}

Chunks:
[1] <first chunk of the transcript>
[2] <second chunk>
```
