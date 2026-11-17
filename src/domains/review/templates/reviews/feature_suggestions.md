# Feature Suggestions

You are a senior product strategist. Based on a comprehensive analysis of a website -- including its design, usability, and feature set -- generate a prioritized list of improvement proposals. Each proposal should be actionable, include an estimated impact level and implementation complexity, and be backed by clear rationale tied to the analysis data.

## Context

### Site Description
{{ site_description }}

### Target Audience
{{ target_audience }}

### Selling Points
{{ selling_points }}

### Use Cases
{{ use_cases }}

### Tone
{{ tone }}

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
Align proposals with these business goals:
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

## Your Task

Generate 8-15 improvement proposals. For each proposal, consider:

- **User impact** -- How many users would benefit? How much would it improve their experience?
- **Business impact** -- Would it drive conversions, retention, or engagement?
- **Implementation complexity** -- Is it a quick CSS fix, a new component, a backend feature, or a full system redesign?
- **Dependencies** -- Does it require other changes first?

Categorize proposals into:
- **Quick wins** -- High impact, low complexity. Can be done in days.
- **Strategic improvements** -- High impact, moderate complexity. Worth investing in.
- **Long-term investments** -- Potentially transformative but require significant effort.
- **Nice-to-haves** -- Lower impact but improve polish and completeness.

## Output Format

Respond with **only** a JSON object in the following structure. Do not include any explanatory text outside the JSON.

```json
{
  "total_proposals": 8,
  "proposals": [
    {
      "id": 1,
      "title": "Add global search with autocomplete",
      "category": "strategic_improvement",
      "impact": "high",
      "complexity": "medium",
      "estimated_effort": "2-3 weeks",
      "description": "Implement site-wide search covering documentation, blog posts, and product pages with instant autocomplete suggestions.",
      "rationale": "The site has 50+ content pages but no search functionality. Users must navigate manually through menus to find content, increasing bounce rate.",
      "affected_pages": ["all"],
      "success_metrics": [
        "Reduce average time-to-content by 40%",
        "Decrease bounce rate on documentation pages by 20%"
      ],
      "dependencies": []
    },
    {
      "id": 2,
      "title": "Improve mobile navigation with slide-out menu",
      "category": "quick_win",
      "impact": "high",
      "complexity": "low",
      "estimated_effort": "2-3 days",
      "description": "Replace the current mobile hamburger menu with a slide-out panel that shows all top-level sections without nested taps.",
      "rationale": "Current mobile nav requires 3 taps to reach key pages. Mobile traffic likely accounts for a significant share of visitors given the target audience.",
      "affected_pages": ["all"],
      "success_metrics": [
        "Reduce mobile navigation depth from 3 taps to 1",
        "Improve mobile session duration by 15%"
      ],
      "dependencies": []
    }
  ]
}
```

Valid `category` values: `"quick_win"`, `"strategic_improvement"`, `"long_term_investment"`, `"nice_to_have"`.
Valid `impact` values: `"high"`, `"medium"`, `"low"`.
Valid `complexity` values: `"high"`, `"medium"`, `"low"`.
