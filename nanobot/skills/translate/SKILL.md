---
name: translate
description: Translate files, documents, or plain text faithfully between languages without summarizing.
---

# Translate

Produce a complete translation, not a summary.

## Rules

- Preserve the source structure: headings, paragraphs, bullet lists, tables, and code fences.
- Keep names, product terms, and technical keywords untranslated when that is the natural convention.
- Match the original tone and register unless the user explicitly asks for adaptation.
- Do not add commentary, notes, or explanations unless the user asks for them.

## Workflow

1. Identify the source language.
2. Confirm the target language if the user did not specify one.
3. Read the full source content before translating.
4. Translate every section in order.
5. If the source is too long for one response, translate it in clearly labeled chunks instead of skipping parts.

## Files And Documents

- For local files, read the file content first and translate the content itself.
- For PDFs or long documents, extract or read text in manageable chunks and keep chunk boundaries explicit.
- Never replace untranslated sections with summaries like "the rest says the same thing".

## What Not To Do

- Do not summarize.
- Do not omit repetitive or boilerplate sections.
- Do not switch to bilingual output unless the user requests it.
