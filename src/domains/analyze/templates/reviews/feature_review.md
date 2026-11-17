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

### Narrative Context
{{ narrative_hooks }}

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
