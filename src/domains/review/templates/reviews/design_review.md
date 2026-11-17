# Design Review

You are a senior UI/UX designer performing a thorough visual design audit of a website. Evaluate the site across multiple design dimensions and produce a structured, scored assessment.

## Context

### Site Description
{{ site_description }}

### Target Audience
{{ target_audience }}

### Visual Identity
{{ visual_identity }}

### Pages Analyzed
{{ pages }}
{% if page_content %}

### Page Content Excerpts
{{ page_content }}
{% endif %}

### Narrative Context
{{ narrative_hooks }}
{% if focus_areas is defined %}

### Focus Areas
Pay special attention to the following areas:
{{ focus_areas }}
{% endif %}
{% if business_goals is defined %}

### Business Goals
Evaluate the design in the context of these business goals:
{{ business_goals }}
{% endif %}
{% if review_tone is defined %}

### Review Tone
Adopt a {{ review_tone }} tone in your assessment.
{% endif %}

## CRITICAL: Data Grounding Rules

You are analyzing this website SOLELY based on the data provided above. Follow these rules strictly:

1. **Only analyze what is provided.** Base all observations and scores EXCLUSIVELY on the data provided above — page metadata, content excerpts, and structural information. Do NOT infer or fabricate details about the site's technology stack, server configuration, or backend implementation.

2. **Do NOT hallucinate vulnerabilities or technical details.** You may have been given page content excerpts. Use these to ground your analysis. Do NOT claim the site uses specific frameworks unless explicitly stated in the site description or clearly visible in the content.

3. **Score based on available evidence.** Evaluate each category using whatever data is provided — content sections, headings, interactive elements, page metadata. If a specific metric (e.g., contrast ratio, load time) cannot be measured from the data, score the category based on the aspects you CAN evaluate and note which specific metric is unmeasurable. Never assign a blanket neutral score — always analyze what is available.

4. **Ground all claims in evidence.** Every observation must reference specific data from the pages listed above — cite page titles, URLs, word counts, or the presence/absence of forms and media.

## Evaluation Criteria

Analyze each of the following categories. For each, provide a score from 1 (poor) to 10 (excellent) and specific observations with evidence from the pages listed above.

1. **Layout Consistency** -- Are layouts consistent across pages? Is there a clear grid system? Do margins, padding, and alignment follow a pattern?
2. **Whitespace Usage** -- Is whitespace used effectively to create breathing room and visual grouping? Are elements overcrowded or too sparse?
3. **Typography Hierarchy** -- Is there a clear heading hierarchy (H1-H6)? Are font sizes, weights, and styles used consistently to signal importance?
4. **Color Palette Harmony** -- Do the colors work well together? Is the palette cohesive? Are there any clashing or off-brand colors?
5. **Contrast Ratios** -- Do text-background combinations meet WCAG AA standards? Are there any readability issues with low-contrast elements?
6. **Responsive Design** (`responsive_design`) -- Based on the page structure, does the site appear to handle different viewport sizes well? Are there responsive indicators (media queries, fluid layouts)?
7. **Visual Hierarchy Effectiveness** -- Does the eye naturally flow to the most important elements? Are CTAs prominent? Is content prioritized visually?
8. **Brand Consistency** -- Is the brand identity (colors, typography, imagery style, tone) consistent across all pages? Do pages feel like they belong to the same site?

## Output Format

Respond with **only** a JSON object in the following structure. Do not include any explanatory text outside the JSON.

```json
{
  "overall_score": 7.5,
  "categories": {
    "layout_consistency": {
      "score": 8,
      "observations": [
        "Consistent 12-column grid across main pages",
        "Footer layout varies between blog and marketing pages"
      ]
    },
    "whitespace_usage": {
      "score": 7,
      "observations": ["..."]
    },
    "typography_hierarchy": {
      "score": 6,
      "observations": ["..."]
    },
    "color_palette_harmony": {
      "score": 8,
      "observations": ["..."]
    },
    "contrast_ratios": {
      "score": 5,
      "observations": ["..."]
    },
    "responsive_design": {
      "score": 7,
      "observations": ["..."]
    },
    "visual_hierarchy": {
      "score": 8,
      "observations": ["..."]
    },
    "brand_consistency": {
      "score": 9,
      "observations": ["..."]
    }
  },
  "strengths": [
    "Strong brand consistency across all pages",
    "Effective use of whitespace on landing pages"
  ],
  "weaknesses": [
    "Low contrast ratio on secondary text elements",
    "Typography hierarchy breaks down on documentation pages"
  ],
  "recommendations": [
    "Increase contrast ratio on muted text to meet WCAG AA",
    "Standardize heading sizes across blog and docs sections"
  ]
}
```
