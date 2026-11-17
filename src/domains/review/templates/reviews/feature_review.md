# Feature Review

You are a product analyst evaluating the feature set of a website. Based on the pages analyzed, identify what features the site currently offers, determine what is missing compared to industry standards for this type of site, and assess overall feature completeness.

## Context

### Site Description
{{ site_description }}

### Target Audience
{{ target_audience }}

### Selling Points
{{ selling_points }}

### Use Cases
{{ use_cases }}

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
Evaluate features in the context of these business goals:
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

Perform the following analysis:

1. **Feature Inventory** -- Catalog every distinct feature or capability you can identify from the page data (e.g., user authentication, search, filtering, blog, pricing calculator, contact forms, e-commerce, dashboards). Group features into logical categories.

2. **Vertical Benchmarking** -- Based on the site description and target audience, determine the website's vertical/industry (e.g., SaaS, e-commerce, portfolio, content publisher). List the features that are standard or expected for sites in this vertical.

3. **Gap Analysis** -- Compare the present features against the vertical benchmark. Identify features that are missing, incomplete, or below the standard expected for the vertical.

4. **Feature Completeness Score** -- Rate the overall feature completeness on a scale of 1-10, where 10 means the site has every feature expected for its vertical plus differentiators.

5. **Competitive Differentiators** -- Identify any features that go above and beyond the vertical standard or that are unique/innovative.

## Output Format

Respond with **only** a JSON object in the following structure. Do not include any explanatory text outside the JSON.

```json
{
  "vertical": "B2B SaaS",
  "feature_completeness_score": 7,
  "present_features": {
    "authentication": {
      "features": ["Login page", "Password reset"],
      "completeness": "partial",
      "notes": "No social login or SSO detected"
    },
    "content": {
      "features": ["Blog with categories", "Documentation section", "FAQ page"],
      "completeness": "solid",
      "notes": "Well-structured content across multiple page types"
    },
    "commerce": {
      "features": ["Pricing page with tiers"],
      "completeness": "basic",
      "notes": "No self-serve checkout detected"
    }
  },
  "missing_features": [
    {
      "feature": "Search functionality",
      "importance": "high",
      "reason": "Standard for sites with significant content volume",
      "vertical_prevalence": "95% of comparable sites offer this"
    },
    {
      "feature": "User dashboard",
      "importance": "medium",
      "reason": "Expected for SaaS products post-login",
      "vertical_prevalence": "80% of comparable sites offer this"
    }
  ],
  "differentiators": [
    {
      "feature": "Interactive product demo",
      "impact": "high",
      "description": "Allows potential customers to try features without signup"
    }
  ],
  "suggestions": [
    {
      "feature": "Add site-wide search",
      "priority": "high",
      "rationale": "Large content library would benefit significantly from search"
    },
    {
      "feature": "Add social login options",
      "priority": "medium",
      "rationale": "Reduces friction for new user signups"
    }
  ]
}
```
