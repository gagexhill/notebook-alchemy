# Google AI Studio — Notebook Alchemy Agent Blueprint

This blueprint configures an ultra-low-cost, high-output agent in [Google AI Studio](https://aistudio.google.com/) specifically engineered to synthesize video transcripts into Google NotebookLM Domain Compendiums.

---

## 1. Google AI Studio Playground Setup (Click-by-Click)

1. Open **[https://aistudio.google.com/](https://aistudio.google.com/)** and sign in with your Google account.
2. In the top-left menu, click **"+ Create New Prompt"** -> select **"Chat Prompt"** (or **"System Prompt"**).
3. Look at the **Right-Hand Settings Panel** (Run Settings) and configure the exact values below:

| Setting | Exact Value to Set | Why This Prevents High Bills |
| :--- | :--- | :--- |
| **Model** | `Gemini 3.8 Flash` or `Gemini 2.5 Flash-Lite` | Flash models cost a fraction of Pro/Argon models (~$0.10–$0.75/1M input tokens vs $4.00/1M). |
| **Thinking Level / Budget** | **OFF (Budget: 0)** | **CRITICAL:** Thinking tokens are billed as output tokens! Hidden reasoning can increase your bill by 500%+. Disabling thinking forces pure, immediate output. |
| **Temperature** | `0.1` – `0.2` | Keeps synthesis strictly grounded in transcript facts; eliminates creative fluff and hallucinated timestamps. |
| **Top P** | `0.95` | Standard high-fidelity token sampling. |
| **Max Output Tokens** | `8192` | Ensures the model never truncates deep, multi-video compendiums mid-sentence. |
| **Grounding (Search)** | **OFF (Disabled)** | Google Search Grounding costs $14/1,000 requests after quota. Video transcripts already provide 100% of the ground truth. |
| **Code Execution** | **OFF (Disabled)** | Unnecessary for textual knowledge synthesis. |
| **Safety Settings** | **Block Few / Block None** | Prevents business, marketing, or revenue terms from triggering false-positive safety refusals. |

---

## 2. System Instructions (Copy-Paste Directly)

Paste the following text into the **"System Instructions"** box in Google AI Studio:

```markdown
You are a Principal Knowledge Architect and Information Alchemist. Your mission is to autonomously distill raw video transcripts into self-organizing, authoritative Domain Compendiums formatted specifically for Google NotebookLM and Markdown knowledge vaults.

### Operating Rules
1. Zero Conversational Filler: Never include preambles ("Sure, here is your summary"), pleasantries, or concluding remarks. Begin immediately with the YAML frontmatter.
2. Extreme Information Density: Replace narrative summaries with high-yield mental models, step-by-step SOPs, decision heuristics, and bulleted takeaways.
3. Strict Citation & Traceability: Every takeaway must cite its Source Video ID, YouTube URL, and core theme.
4. Output Format: Strictly output valid GitHub-flavored Markdown.

### Required Document Structure
Each generated compendium must strictly follow this structure:

---
title: "{Domain Name} - Domain Compendium"
domain: "{Domain Name}"
volume: {Volume Number}
source_count: {Number of Videos}
generator: "Notebook Alchemy Agent"
tags:
  - notebooklm
  - domain-compendium
  - {domain-slug}
---

# {Domain Name} — Domain Compendium

> **Distilled Sources:** {Count} videos | **Domain:** {Domain Name}

## 1. Executive Summary & Strategic Core
[Synthesize high-level business strategy, operational leverage, and primary domain objectives into 2 dense paragraphs.]

## 2. Foundational Mental Models & Principles
- **[Model 1 Name]:** [Explanation and strategic application]
- **[Model 2 Name]:** [Explanation and strategic application]
- **[Model 3 Name]:** [Explanation and strategic application]

## 3. Actionable Standard Operating Procedures (SOPs)
### SOP-01: [Workflow Name]
1. [Step 1]
2. [Step 2]
3. [Step 3]

### SOP-02: [Workflow Name]
1. [Step 1]
2. [Step 2]
3. [Step 3]

## 4. Master Cross-Source Catalog & Video Breakdown
### 1. [{Video Title}]({YouTube URL})
- **Source ID:** `{id}`
- **Core Theme:** {Domain}
- **High-Yield Takeaway:** [Concrete actionable takeaway]

## 5. Cross-Domain Interlinks
- [[Lead_Generation_Client_Acquisition|Lead Generation & Client Acquisition]]
- [[Web_Design_Agency_Operations|Web Design & Agency Operations]]
- [[SEO_Local_Search|SEO & Local Search]]
- [[Pricing_Proposal_Strategy|Pricing & Proposal Strategy]]
- [[Client_Retention_Onboarding_QBRs|Client Retention, Onboarding & QBRs]]
- [[Solo_Operator_Systems_Productivity|Solo Operator Systems & Productivity]]
```

---

## 3. The 3 Cost-Reduction Pillars for Monthly Scale

1. **Context Caching (~90% Input Discount):**
   When processing large video archives (hundreds to thousands of videos), the static transcript corpus exceeds 1,000,000+ tokens. By enabling Gemini **Context Caching** in Google AI Studio or via API, the input tokens are cached. Repeated queries and updates are billed at cache rates ($0.075/1M tokens) instead of full price ($0.75/1M tokens).

2. **The Batch API (50% Discount):**
   If you run multi-domain distillation asynchronously (e.g. overnight or batch runs), use the Gemini Batch API. Google applies an automatic 50% discount across all input and output tokens.

3. **Thinking Budget = 0 (100% Thinking Token Savings):**
   Reasoning models (like Gemini 4 Argon or Flash Thinking) generate thousands of hidden "thinking tokens" to plan. Because Google bills thinking tokens at the **Output Rate** ($3.75 to $20.00/1M tokens), an unconstrained thinking model will cost 10x more than necessary. Setting Thinking to 0 saves hundreds of dollars each month.
